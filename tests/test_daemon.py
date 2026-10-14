import json
import logging
import os
import signal
import threading
from datetime import UTC, datetime, timedelta

import pytest

from timeexisting import paths
from timeexisting.collector import daemon, lockfile
from timeexisting.ledger import store
from timeexisting.ledger.events import Confidence, Event, EventType, Source, profile_for
from timeexisting.ledger.replay import IntervalType, replay
from timeexisting.presence.backend import ChannelCheck, SessionState

_T0 = datetime(2026, 9, 17, 6, 0, 0, tzinfo=UTC)


class _ManualClock:
    """A clock that only moves when the fake `sleep` moves it, by the requested seconds plus any queued jump."""

    def __init__(self, start: datetime) -> None:
        self.current = start
        self.jumps: dict[int, timedelta] = {}
        self.sleeps = 0

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.current += timedelta(seconds=seconds) + self.jumps.get(self.sleeps, timedelta())


def _host() -> str:
    return paths.current_host()


def _ledger() -> tuple[Event, ...]:
    return store.read_shard(store.shard_path(_host())).events


def _summary() -> list[tuple[str, str, datetime, str]]:
    return [(str(e.event), e.data.get("reason", ""), e.ts, str(e.confidence)) for e in _ledger()]


def _write_checkpoint_file(ts: datetime | str) -> None:
    value = ts.isoformat() if isinstance(ts, datetime) else ts
    paths.checkpoint_file(_host()).write_text(json.dumps({"ts": value, "pid": 1}), encoding="utf-8")


def _append(
    event: EventType, ts: datetime, reason: str, confidence: Confidence = Confidence.OBSERVED
) -> None:
    store.append(
        Event.new(
            ts=ts,
            host=_host(),
            profile="work",
            event=event,
            source=Source.COLLECTOR,
            confidence=confidence,
            data={"reason": reason},
        )
    )


def _run(cfg, clock: _ManualClock, **kwargs) -> daemon.StopReason:
    return daemon.run_collector(clock, clock.sleep, cfg, **kwargs)


def test_n_ticks_write_only_start_and_stop(cfg):
    clock = _ManualClock(_T0)
    assert _run(cfg, clock, max_ticks=120) is daemon.StopReason.MAX_TICKS

    events = _ledger()
    assert [(str(e.event), e.data["reason"]) for e in events] == [
        ("collector_start", "launch"),
        ("collector_stop", "max_ticks"),
    ]
    assert all(e.source is Source.COLLECTOR and e.confidence is Confidence.OBSERVED for e in events)
    assert all(e.host == _host() and e.profile == profile_for(_host(), cfg) for e in events)
    assert events[0].ts == _T0
    assert events[1].ts == _T0 + 120 * cfg.collector.tick
    assert clock.sleeps == 120


def test_sleep_is_asked_for_one_tick(cfg):
    requested: list[float] = []
    clock = _ManualClock(_T0)

    def sleep(seconds: float) -> None:
        requested.append(seconds)
        clock.sleep(seconds)

    daemon.run_collector(clock, sleep, cfg, max_ticks=3)
    assert requested == [cfg.collector.tick.total_seconds()] * 3


def test_checkpoint_tracks_every_tick_and_a_clean_stop_deletes_it(cfg):
    clock = _ManualClock(_T0)
    seen: list[dict] = []

    def sleep(seconds: float) -> None:
        seen.append(json.loads(paths.checkpoint_file(_host()).read_text(encoding="utf-8")))
        clock.sleep(seconds)

    daemon.run_collector(clock, sleep, cfg, max_ticks=5)

    assert [record["ts"] for record in seen] == [(_T0 + n * cfg.collector.tick).isoformat() for n in range(5)]
    assert all(record["pid"] == os.getpid() for record in seen)
    assert not paths.checkpoint_file(_host()).exists()
    assert not paths.checkpoint_file(_host()).with_name(f"checkpoint-{_host()}.json.tmp").exists()


def test_lock_is_held_while_running_and_released_after(cfg):
    clock = _ManualClock(_T0)
    held: list[int | None] = []

    def sleep(seconds: float) -> None:
        owner = lockfile.status("collector")
        held.append(owner.pid if owner else None)
        clock.sleep(seconds)

    daemon.run_collector(clock, sleep, cfg, max_ticks=2)
    assert held == [os.getpid(), os.getpid()]
    assert lockfile.status("collector") is None


def test_a_held_lock_refuses_before_writing_anything(cfg):
    lockfile.acquire("collector")
    with pytest.raises(lockfile.AlreadyRunning):
        _run(cfg, _ManualClock(_T0), max_ticks=1)
    assert _ledger() == ()
    assert not paths.checkpoint_file(_host()).exists()


def test_recovery_writes_an_inferred_unclean_stop_before_the_start(cfg):
    _append(EventType.COLLECTOR_START, _T0 - timedelta(hours=2), "launch")
    crashed_at = _T0 - timedelta(hours=1)
    _write_checkpoint_file(crashed_at)

    _run(cfg, _ManualClock(_T0), max_ticks=1)

    assert _summary()[1:3] == [
        ("collector_stop", "unclean", crashed_at, "inferred"),
        ("collector_start", "launch", _T0, "observed"),
    ]
    assert not paths.checkpoint_file(_host()).exists()


def test_recovery_on_an_empty_ledger_still_infers_the_stop(cfg):
    _write_checkpoint_file(_T0 - timedelta(minutes=5))
    _run(cfg, _ManualClock(_T0), max_ticks=0)
    assert _summary()[0] == ("collector_stop", "unclean", _T0 - timedelta(minutes=5), "inferred")


@pytest.mark.parametrize("stopped_after", [timedelta(0), timedelta(seconds=12)])
def test_recovery_skips_a_run_that_already_stopped_cleanly(caplog, stopped_after):
    last_tick = _T0 - timedelta(hours=1)
    _append(EventType.COLLECTOR_START, last_tick - timedelta(hours=1), "launch")
    _append(EventType.COLLECTOR_STOP, last_tick + stopped_after, "signal")
    _write_checkpoint_file(last_tick)
    before = _summary()

    with caplog.at_level(logging.INFO, logger="timeexisting.collector.daemon"):
        daemon.recover(_host(), "work")

    assert _summary() == before
    assert not paths.checkpoint_file(_host()).exists()
    assert "only its checkpoint was left behind" in caplog.text


def test_recovery_still_infers_when_the_last_stop_is_older_than_the_checkpoint():
    last_tick = _T0 - timedelta(hours=1)
    _append(EventType.COLLECTOR_STOP, last_tick - timedelta(seconds=1), "signal")
    _write_checkpoint_file(last_tick)

    daemon.recover(_host(), "work")

    assert _summary()[-1] == ("collector_stop", "unclean", last_tick, "inferred")


def test_recovery_still_infers_when_the_last_event_is_a_start():
    last_tick = _T0 - timedelta(hours=1)
    _append(EventType.COLLECTOR_START, last_tick + timedelta(minutes=5), "launch")
    _write_checkpoint_file(last_tick)

    daemon.recover(_host(), "work")

    assert _summary()[-1] == ("collector_stop", "unclean", last_tick, "inferred")


@pytest.mark.parametrize(
    "content", ["not json", "{}", '{"ts": 5}', '{"ts": "yesterday"}', '{"ts": "2026-09-17T06:00:00"}']
)
def test_unparseable_checkpoint_is_deleted_and_writes_nothing(caplog, content):
    paths.checkpoint_file(_host()).write_text(content, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="timeexisting.collector.daemon"):
        daemon.recover(_host(), "work")
    assert _ledger() == ()
    assert not paths.checkpoint_file(_host()).exists()
    assert "without recovery" in caplog.text


def test_no_checkpoint_means_no_recovery():
    daemon.recover(_host(), "work")
    assert _ledger() == ()


def test_clock_jump_over_two_ticks_writes_the_suspend_pair(cfg):
    tick = cfg.collector.tick
    clock = _ManualClock(_T0)
    clock.jumps[3] = timedelta(minutes=5)

    _run(cfg, clock, max_ticks=5)

    before_sleep = _T0 + 2 * tick
    woke = _T0 + 3 * tick + timedelta(minutes=5)
    assert _summary() == [
        ("collector_start", "launch", _T0, "observed"),
        ("collector_stop", "suspended", before_sleep, "inferred"),
        ("collector_start", "resumed", woke, "observed"),
        ("collector_stop", "max_ticks", woke + 2 * tick, "observed"),
    ]


def test_clock_jump_of_exactly_two_ticks_is_not_a_suspend(cfg):
    clock = _ManualClock(_T0)
    clock.jumps[2] = cfg.collector.tick
    _run(cfg, clock, max_ticks=4)
    assert [reason for _, reason, _, _ in _summary()] == ["launch", "max_ticks"]


def test_clock_moving_backwards_is_logged_and_not_a_suspend(cfg, caplog):
    clock = _ManualClock(_T0)
    clock.jumps[2] = -timedelta(hours=1)
    with caplog.at_level(logging.WARNING, logger="timeexisting.collector.daemon"):
        _run(cfg, clock, max_ticks=4)
    assert [reason for _, reason, _, _ in _summary()] == ["launch", "max_ticks"]
    assert "backwards" in caplog.text


def test_permission_error_on_replace_is_logged_and_the_next_tick_lands(cfg, caplog, monkeypatch):
    clock = _ManualClock(_T0)
    real_replace = os.replace
    failures = iter([True])

    def flaky_replace(source, target):
        if next(failures, False):
            raise PermissionError("held open by something else")
        real_replace(source, target)

    seen: list[str] = []

    def sleep(seconds: float) -> None:
        checkpoint = paths.checkpoint_file(_host())
        seen.append(json.loads(checkpoint.read_text(encoding="utf-8"))["ts"] if checkpoint.exists() else "")
        clock.sleep(seconds)

    monkeypatch.setattr(daemon.os, "replace", flaky_replace)
    with caplog.at_level(logging.WARNING, logger="timeexisting.collector.daemon"):
        daemon.run_collector(clock, sleep, cfg, max_ticks=2)

    assert seen == ["", (_T0 + cfg.collector.tick).isoformat()]
    assert "retrying next tick" in caplog.text
    assert [reason for _, reason, _, _ in _summary()] == ["launch", "max_ticks"]


def test_ticking_across_the_autumn_fall_back_writes_no_suspend(cfg):
    # 25 October 2026, 01:00 UTC: Copenhagen goes from 03:00 CEST back to 02:00 CET.
    clock = _ManualClock(datetime(2026, 10, 24, 23, 0, tzinfo=UTC))
    ticks = int(timedelta(hours=4) / cfg.collector.tick)
    _run(cfg, clock, max_ticks=ticks)
    assert [reason for _, reason, _, _ in _summary()] == ["launch", "max_ticks"]


def test_ticking_across_local_midnight_is_one_run(cfg):
    clock = _ManualClock(datetime(2026, 9, 17, 21, 30, tzinfo=UTC))
    _run(cfg, clock, max_ticks=int(timedelta(hours=1) / cfg.collector.tick))
    assert [reason for _, reason, _, _ in _summary()] == ["launch", "max_ticks"]


def test_stop_file_ends_the_loop_within_one_tick(cfg):
    clock = _ManualClock(_T0)

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if clock.sleeps == 3:
            paths.stop_file().touch()

    reason = daemon.run_collector(clock, sleep, cfg, max_ticks=100)

    assert reason is daemon.StopReason.STOP_FILE
    assert clock.sleeps == 3
    assert _summary()[-1] == ("collector_stop", "stop_file", _T0 + 3 * cfg.collector.tick, "observed")
    assert not paths.stop_file().exists()
    assert not paths.checkpoint_file(_host()).exists()


def test_stop_event_ends_the_loop_with_reason_signal(cfg):
    clock = _ManualClock(_T0)
    stop_event = threading.Event()

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if clock.sleeps == 2:
            stop_event.set()

    reason = daemon.run_collector(clock, sleep, cfg, max_ticks=100, stop_event=stop_event)

    assert reason is daemon.StopReason.SIGNAL
    assert _summary()[-1] == ("collector_stop", "signal", _T0 + 2 * cfg.collector.tick, "observed")


def test_a_stale_stop_file_does_not_stop_a_new_collector(cfg):
    paths.stop_file().touch()
    assert _run(cfg, _ManualClock(_T0), max_ticks=2) is daemon.StopReason.MAX_TICKS


def test_an_unexpected_error_leaves_the_checkpoint_and_releases_the_lock(cfg):
    clock = _ManualClock(_T0)

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if clock.sleeps == 2:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        daemon.run_collector(clock, sleep, cfg, max_ticks=10)

    assert [reason for _, reason, _, _ in _summary()] == ["launch"]
    assert paths.checkpoint_file(_host()).exists()
    assert lockfile.status("collector") is None

    _run(cfg, _ManualClock(_T0 + timedelta(hours=1)), max_ticks=0)
    assert _summary()[1] == ("collector_stop", "unclean", _T0 + cfg.collector.tick, "inferred")


def test_interruptible_sleep_returns_early_once_the_event_is_set():
    stop_event = threading.Event()
    stop_event.set()
    daemon.interruptible_sleep(stop_event)(3600)


def test_signal_handlers_set_the_event_and_are_restored():
    stop_event = threading.Event()
    before = signal.getsignal(signal.SIGINT)
    with daemon.signal_handlers(stop_event):
        signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
    assert stop_event.is_set()
    assert signal.getsignal(signal.SIGINT) is before


def test_shutdown_from_another_thread_ends_the_run_with_its_reason(cfg):
    clock = _ManualClock(_T0)
    captured: list[daemon.Shutdown] = []
    checkpoints_after: list[bool] = []

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if clock.sleeps == 2:
            worker = threading.Thread(target=captured[0], args=(daemon.StopReason.SESSION_END,))
            worker.start()
            worker.join()
            checkpoints_after.append(paths.checkpoint_file(_host()).exists())

    reason = daemon.run_collector(
        clock, sleep, cfg, max_ticks=100, on_start=lambda shutdown, _inbox: captured.append(shutdown)
    )

    assert reason is daemon.StopReason.SESSION_END
    assert clock.sleeps == 2
    assert _summary() == [
        ("collector_start", "launch", _T0, "observed"),
        ("collector_stop", "session_end", _T0 + 2 * cfg.collector.tick, "observed"),
    ]
    assert checkpoints_after == [False]
    assert not paths.checkpoint_file(_host()).exists()
    assert lockfile.status("collector") is None


def test_on_start_runs_after_the_start_and_first_checkpoint(cfg):
    seen: list[tuple[int, bool]] = []

    def on_start(_shutdown: daemon.Shutdown, _inbox: daemon.Inbox) -> None:
        seen.append((len(_ledger()), paths.checkpoint_file(_host()).exists()))

    _run(cfg, _ManualClock(_T0), max_ticks=0, on_start=on_start)
    assert seen == [(1, True)]


def test_shutdown_is_idempotent(cfg):
    clock = _ManualClock(_T0)
    results: list[bool] = []

    def on_start(shutdown: daemon.Shutdown, _inbox: daemon.Inbox) -> None:
        results.append(shutdown(daemon.StopReason.SESSION_END))
        results.append(shutdown(daemon.StopReason.CONSOLE_CLOSE))

    reason = _run(cfg, clock, max_ticks=5, on_start=on_start)

    assert results == [True, False]
    assert reason is daemon.StopReason.SESSION_END
    assert clock.sleeps == 0
    assert [reason for _, reason, _, _ in _summary()] == ["launch", "session_end"]


def test_concurrent_shutdowns_write_one_stop(cfg):
    results: list[bool] = []

    def on_start(shutdown: daemon.Shutdown, _inbox: daemon.Inbox) -> None:
        barrier = threading.Barrier(8)

        def call() -> None:
            barrier.wait()
            results.append(shutdown(daemon.StopReason.SESSION_END))

        workers = [threading.Thread(target=call) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

    _run(cfg, _ManualClock(_T0), max_ticks=5, on_start=on_start)

    assert sorted(results) == [False] * 7 + [True]
    assert [reason for _, reason, _, _ in _summary()] == ["launch", "session_end"]


class _FakeEventLog:
    """An event log reader whose startup self-check answers `channels`, or raises when `channels` is an exception."""

    def __init__(self, channels: ChannelCheck | Exception | None) -> None:
        self.channels = channels
        self.checks = 0

    def check(self) -> ChannelCheck | None:
        self.checks += 1
        if isinstance(self.channels, Exception):
            raise self.channels
        return self.channels

    def system_boundaries(self, _since: datetime, _until: datetime) -> list:
        raise AssertionError("the collector reads no System records in step 4")

    def session_records(self, _since: datetime) -> list:
        raise AssertionError(
            "the collector reads no Winlogon records before the step 5 live check confirms the mapping"
        )


def _notes() -> list[Event]:
    return [event for event in _ledger() if event.event is EventType.NOTE]


def test_an_unreadable_winlogon_channel_writes_one_wts_only_note_after_the_start(cfg, caplog):
    event_log = _FakeEventLog(ChannelCheck(system=True, winlogon=False))
    with caplog.at_level(logging.WARNING, logger=daemon.__name__):
        _run(cfg, _ManualClock(_T0), max_ticks=3, event_log=event_log)

    assert event_log.checks == 1
    assert [str(event.event) for event in _ledger()] == ["collector_start", "note", "collector_stop"]
    (note,) = _notes()
    assert dict(note.data) == {"kind": "wts_only"}
    assert (note.ts, note.source, note.confidence) == (_T0, Source.COLLECTOR, Confidence.OBSERVED)
    assert "WTS-only" in caplog.text


def test_readable_channels_write_nothing(cfg):
    event_log = _FakeEventLog(ChannelCheck(system=True, winlogon=True))
    _run(cfg, _ManualClock(_T0), max_ticks=3, event_log=event_log)
    assert event_log.checks == 1
    assert _notes() == []


def test_an_unreadable_system_log_is_logged_and_writes_nothing(cfg, caplog):
    with caplog.at_level(logging.WARNING, logger=daemon.__name__):
        _run(
            cfg,
            _ManualClock(_T0),
            max_ticks=3,
            event_log=_FakeEventLog(ChannelCheck(system=False, winlogon=True)),
        )
    assert _notes() == []
    assert "clock-jump path" in caplog.text


def test_both_channels_unreadable_write_one_note_and_the_collector_runs_on(cfg):
    clock = _ManualClock(_T0)
    reason = _run(
        cfg, clock, max_ticks=3, event_log=_FakeEventLog(ChannelCheck(system=False, winlogon=False))
    )
    assert reason is daemon.StopReason.MAX_TICKS
    assert clock.sleeps == 3
    assert len(_notes()) == 1


def test_no_event_log_on_this_platform_writes_nothing(cfg):
    _run(cfg, _ManualClock(_T0), max_ticks=1, event_log=_FakeEventLog(None))
    assert _notes() == []


def test_a_failing_self_check_never_stops_the_collector(cfg, caplog):
    clock = _ManualClock(_T0)
    with caplog.at_level(logging.ERROR, logger=daemon.__name__):
        reason = _run(cfg, clock, max_ticks=3, event_log=_FakeEventLog(RuntimeError("EvtQuery exploded")))
    assert reason is daemon.StopReason.MAX_TICKS
    assert clock.sleeps == 3
    assert _notes() == []
    assert "self-check failed" in caplog.text


def test_the_self_check_runs_before_on_start(cfg):
    order: list[str] = []
    event_log = _FakeEventLog(ChannelCheck(system=True, winlogon=True))
    original = event_log.check

    def check() -> ChannelCheck | None:
        order.append("check")
        return original()

    event_log.check = check
    _run(
        cfg,
        _ManualClock(_T0),
        max_ticks=0,
        event_log=event_log,
        on_start=lambda _shutdown, _inbox: order.append("on_start"),
    )
    assert order == ["check", "on_start"]


# Step 5: the inbox (single writer), the startup probe and the two-tick reconciliation.

_LOCKED = SessionState.LOCKED
_UNLOCKED = SessionState.UNLOCKED


class _FakeSession:
    """A session probe answering `readings` in order, the first at the startup probe and one per tick after it; the last reading repeats. `logon` is the session's logon time."""

    def __init__(self, readings: list[SessionState | None], logon: datetime | None = None) -> None:
        self.readings = readings
        self.logon = logon
        self.reads = 0

    def state(self) -> SessionState | None:
        reading = self.readings[min(self.reads, len(self.readings) - 1)]
        self.reads += 1
        return reading

    def logon_time(self) -> datetime | None:
        return self.logon


def _session_lines() -> list[tuple[str, str, datetime, str, dict]]:
    return [
        (str(e.event), str(e.source), e.ts, str(e.confidence), dict(e.data))
        for e in _ledger()
        if e.event in {EventType.LOCK, EventType.UNLOCK, EventType.SUSPEND, EventType.RESUME}
    ]


def _tick(cfg, n: int) -> datetime:
    return _T0 + n * cfg.collector.tick


def test_a_transition_put_from_another_thread_is_written_by_the_next_tick_not_by_the_put(cfg):
    clock = _ManualClock(_T0)
    inboxes: list[daemon.Inbox] = []
    after_put: list[int] = []

    def sleep(seconds: float) -> None:
        if clock.sleeps == 0:
            clock.current += timedelta(seconds=10)
            worker = threading.Thread(target=inboxes[0].put, args=(EventType.LOCK, Source.WTS))
            worker.start()
            worker.join()
            after_put.append(len(_ledger()))
            clock.current -= timedelta(seconds=10)
        clock.sleep(seconds)

    daemon.run_collector(
        clock, sleep, cfg, max_ticks=2, on_start=lambda _shutdown, inbox: inboxes.append(inbox)
    )

    assert after_put == [1]
    assert [str(e.event) for e in _ledger()] == ["collector_start", "lock", "collector_stop"]
    assert _session_lines() == [("lock", "wts", _T0 + timedelta(seconds=10), "observed", {})]


def test_transitions_are_written_in_arrival_order_with_their_receipt_times(cfg):
    clock = _ManualClock(_T0)

    def on_start(_shutdown: daemon.Shutdown, inbox: daemon.Inbox) -> None:
        inbox.put(EventType.SUSPEND, Source.POWER, "PBT_APMSUSPEND")
        clock.current += timedelta(seconds=5)
        inbox.put(EventType.RESUME, Source.POWER, "PBT_APMRESUMEAUTOMATIC")
        inbox.put(EventType.UNLOCK, Source.WTS)
        clock.current -= timedelta(seconds=5)

    _run(cfg, clock, max_ticks=1, on_start=on_start)

    assert _session_lines() == [
        ("suspend", "power", _T0, "observed", {"detail": "PBT_APMSUSPEND"}),
        ("resume", "power", _T0 + timedelta(seconds=5), "observed", {"detail": "PBT_APMRESUMEAUTOMATIC"}),
        ("unlock", "wts", _T0 + timedelta(seconds=5), "observed", {}),
    ]


def test_the_shutdown_drains_the_inbox_before_its_stop(cfg):
    def on_start(shutdown: daemon.Shutdown, inbox: daemon.Inbox) -> None:
        inbox.put(EventType.UNLOCK, Source.WTS)
        shutdown(daemon.StopReason.SESSION_END)

    _run(cfg, _ManualClock(_T0), max_ticks=5, on_start=on_start)
    assert [str(e.event) for e in _ledger()] == ["collector_start", "unlock", "collector_stop"]


def test_an_empty_inbox_writes_nothing(cfg):
    _run(cfg, _ManualClock(_T0), max_ticks=3, on_start=lambda _shutdown, _inbox: None)
    assert [str(e.event) for e in _ledger()] == ["collector_start", "collector_stop"]


def test_startup_probe_unlocked_with_a_logon_today_writes_unlock_from_logon_at_the_logon_time(cfg):
    logon = _T0 - timedelta(hours=1)
    _run(cfg, _ManualClock(_T0), max_ticks=1, session=_FakeSession([_UNLOCKED], logon))
    assert _session_lines() == [("unlock", "logon", logon, "observed", {})]


def test_startup_probe_unlocked_with_a_logon_on_an_earlier_day_writes_an_inferred_probe_unlock(cfg):
    logon = _T0 - timedelta(days=3)
    _run(cfg, _ManualClock(_T0), max_ticks=1, session=_FakeSession([_UNLOCKED], logon))
    assert _session_lines() == [("unlock", "probe", _T0, "inferred", {})]


@pytest.mark.parametrize(
    ("logon", "source"),
    [
        # 00:30 local on the same day as the 08:00 local start, though the day before in UTC.
        (datetime(2026, 9, 16, 22, 30, tzinfo=UTC), "logon"),
        # 23:30 local the evening before.
        (datetime(2026, 9, 16, 21, 30, tzinfo=UTC), "probe"),
    ],
)
def test_startup_probe_reads_today_in_local_time_not_utc(cfg, logon, source):
    _run(cfg, _ManualClock(_T0), max_ticks=1, session=_FakeSession([_UNLOCKED], logon))
    assert [line[1] for line in _session_lines()] == [source]


def test_startup_probe_on_the_dst_end_day_keeps_the_logon_time_in_utc(cfg):
    start = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
    logon = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # 02:30 local, CEST, before the clocks went back
    _run(cfg, _ManualClock(start), max_ticks=1, session=_FakeSession([_UNLOCKED], logon))
    assert _session_lines() == [("unlock", "logon", logon, "observed", {})]


def test_a_first_start_after_a_logon_today_writes_unlock_from_logon_at_the_logon_time(cfg):
    """The previous run stopped last night, before this morning's logon."""
    _append(EventType.COLLECTOR_START, _T0 - timedelta(hours=14), "launch")
    _append(EventType.COLLECTOR_STOP, _T0 - timedelta(hours=10), "session_end")
    logon = _T0 - timedelta(minutes=5)
    _run(cfg, _ManualClock(_T0), max_ticks=1, session=_FakeSession([_UNLOCKED], logon))
    assert _session_lines() == [("unlock", "logon", logon, "observed", {})]


_LOGON_TODAY = _T0 - timedelta(hours=1)
_RESTART = _T0 + timedelta(hours=2)


def _restart_after_a_clean_stop(cfg) -> None:
    _run(cfg, _ManualClock(_T0), max_ticks=10, session=_FakeSession([_UNLOCKED], _LOGON_TODAY))


def _restart_after_an_unclean_stop(_cfg) -> None:
    """A run that crashed: its start and logon unlock are in the ledger, its last checkpoint is left behind."""
    _append(EventType.COLLECTOR_START, _T0, "launch")
    store.append(
        Event.new(
            ts=_LOGON_TODAY,
            host=_host(),
            profile="work",
            event=EventType.UNLOCK,
            source=Source.LOGON,
            confidence=Confidence.OBSERVED,
        )
    )
    _write_checkpoint_file(_T0 + timedelta(minutes=30))


@pytest.mark.parametrize("earlier_run", [_restart_after_a_clean_stop, _restart_after_an_unclean_stop])
def test_a_restart_later_the_same_day_writes_an_inferred_probe_unlock_at_the_restart(cfg, earlier_run):
    earlier_run(cfg)
    _run(cfg, _ManualClock(_RESTART), max_ticks=1, session=_FakeSession([_UNLOCKED], _LOGON_TODAY))
    assert _session_lines() == [
        ("unlock", "logon", _LOGON_TODAY, "observed", {}),
        ("unlock", "probe", _RESTART, "inferred", {}),
    ]


@pytest.mark.parametrize("earlier_run", [_restart_after_a_clean_stop, _restart_after_an_unclean_stop])
def test_replay_of_a_same_day_restart_is_present_from_the_restart_inferred(cfg, earlier_run):
    earlier_run(cfg)
    _run(cfg, _ManualClock(_RESTART), max_ticks=1, session=_FakeSession([_UNLOCKED], _LOGON_TODAY))
    restart_stop = _RESTART + cfg.collector.tick
    restart_run = [i for i in replay(_ledger()).intervals if _RESTART <= i.start < restart_stop]
    assert [(i.kind, i.start, i.end, i.confidence, i.source) for i in restart_run] == [
        (IntervalType.PRESENT, _RESTART, restart_stop, Confidence.INFERRED, Source.PROBE)
    ]


@pytest.mark.parametrize("logon", [None, _T0 + timedelta(minutes=1)])
def test_startup_probe_without_a_usable_logon_time_writes_an_inferred_probe_unlock(cfg, logon):
    _run(cfg, _ManualClock(_T0), max_ticks=1, session=_FakeSession([_UNLOCKED], logon))
    assert _session_lines() == [("unlock", "probe", _T0, "inferred", {})]


def test_startup_probe_on_a_locked_session_writes_nothing_and_records_the_state(cfg):
    clock = _ManualClock(_T0)
    seen: list[SessionState | None] = []

    def sleep(seconds: float) -> None:
        seen.append(daemon.read_checkpoint_session(_host()))
        clock.sleep(seconds)

    daemon.run_collector(clock, sleep, cfg, max_ticks=1, session=_FakeSession([_LOCKED], _T0))
    assert _session_lines() == []
    assert seen == [_LOCKED]


def test_startup_probe_with_an_unknown_state_writes_nothing_and_records_none(cfg):
    clock = _ManualClock(_T0)
    seen: list[dict] = []

    def sleep(seconds: float) -> None:
        seen.append(json.loads(paths.checkpoint_file(_host()).read_text(encoding="utf-8")))
        clock.sleep(seconds)

    daemon.run_collector(clock, sleep, cfg, max_ticks=1, session=_FakeSession([None]))
    assert _session_lines() == []
    assert "session" not in seen[0]


def test_the_startup_probe_runs_after_the_start_and_before_on_start(cfg):
    seen: list[list[str]] = []
    _run(
        cfg,
        _ManualClock(_T0),
        max_ticks=0,
        session=_FakeSession([_UNLOCKED]),
        on_start=lambda _shutdown, _inbox: seen.append([str(e.event) for e in _ledger()]),
    )
    assert seen == [["collector_start", "unlock"]]


def test_one_disagreeing_tick_writes_nothing(cfg):
    """A single disagreeing reading between two agreeing ones."""
    session = _FakeSession([_UNLOCKED, _UNLOCKED, _LOCKED, _UNLOCKED, _UNLOCKED])
    _run(cfg, _ManualClock(_T0), max_ticks=4, session=session)
    assert _session_lines() == [("unlock", "probe", _T0, "inferred", {})]


def test_two_consecutive_disagreeing_ticks_write_the_missing_lock_at_the_first(cfg):
    session = _FakeSession([_UNLOCKED, _UNLOCKED, _LOCKED, _LOCKED, _LOCKED])
    _run(cfg, _ManualClock(_T0), max_ticks=4, session=session)
    assert _session_lines() == [
        ("unlock", "probe", _T0, "inferred", {}),
        ("lock", "probe", _tick(cfg, 2), "inferred", {}),
    ]


def test_two_consecutive_disagreeing_ticks_write_the_missing_unlock(cfg):
    session = _FakeSession([_LOCKED, _UNLOCKED, _UNLOCKED])
    _run(cfg, _ManualClock(_T0), max_ticks=3, session=session)
    assert _session_lines() == [("unlock", "probe", _tick(cfg, 1), "inferred", {})]


def test_disagreements_that_are_not_consecutive_write_nothing(cfg):
    session = _FakeSession([_UNLOCKED, _LOCKED, _UNLOCKED, _LOCKED, None, _LOCKED, _UNLOCKED])
    _run(cfg, _ManualClock(_T0), max_ticks=6, session=session)
    assert [line[1] for line in _session_lines()] == ["probe"]


def test_a_wts_transition_drained_in_the_tick_agrees_with_the_probe_and_no_probe_line_is_written(cfg):
    clock = _ManualClock(_T0)
    inboxes: list[daemon.Inbox] = []

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if clock.sleeps == 1:
            inboxes[0].put(EventType.LOCK, Source.WTS)
        if clock.sleeps == 3:
            inboxes[0].put(EventType.UNLOCK, Source.WTS)

    session = _FakeSession([_UNLOCKED, _LOCKED, _LOCKED, _UNLOCKED, _UNLOCKED])
    daemon.run_collector(
        clock,
        sleep,
        cfg,
        max_ticks=4,
        session=session,
        on_start=lambda _shutdown, inbox: inboxes.append(inbox),
    )
    assert [(line[0], line[1]) for line in _session_lines()] == [
        ("unlock", "probe"),
        ("lock", "wts"),
        ("unlock", "wts"),
    ]


def test_a_late_wts_transition_is_still_written_after_the_probe_wrote_its_own(cfg):
    clock = _ManualClock(_T0)
    inboxes: list[daemon.Inbox] = []

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if clock.sleeps == 3:
            inboxes[0].put(EventType.LOCK, Source.WTS)

    session = _FakeSession([_UNLOCKED, _LOCKED])
    daemon.run_collector(
        clock,
        sleep,
        cfg,
        max_ticks=4,
        session=session,
        on_start=lambda _shutdown, inbox: inboxes.append(inbox),
    )
    assert [(line[0], line[1], line[2]) for line in _session_lines()] == [
        ("unlock", "probe", _T0),
        ("lock", "probe", _tick(cfg, 1)),
        ("lock", "wts", _tick(cfg, 3)),
    ]


def test_the_checkpoint_holds_the_last_known_session_state_every_tick(cfg):
    clock = _ManualClock(_T0)
    seen: list[SessionState | None] = []

    def sleep(seconds: float) -> None:
        seen.append(daemon.read_checkpoint_session(_host()))
        clock.sleep(seconds)

    session = _FakeSession([_UNLOCKED, _LOCKED, _LOCKED, _LOCKED])
    daemon.run_collector(clock, sleep, cfg, max_ticks=4, session=session)
    assert seen == [_UNLOCKED, _UNLOCKED, _LOCKED, _LOCKED]


def test_without_a_session_probe_nothing_is_probed_and_the_checkpoint_holds_no_state(cfg):
    clock = _ManualClock(_T0)
    seen: list[dict] = []

    def sleep(seconds: float) -> None:
        seen.append(json.loads(paths.checkpoint_file(_host()).read_text(encoding="utf-8")))
        clock.sleep(seconds)

    daemon.run_collector(clock, sleep, cfg, max_ticks=3)
    assert _session_lines() == []
    assert all("session" not in record for record in seen)


@pytest.mark.parametrize("content", ["", "{", '{"ts": "2026-09-17T06:00:00+00:00"}', '{"session": "asleep"}'])
def test_an_unreadable_checkpoint_session_is_none(content):
    paths.checkpoint_file(_host()).parent.mkdir(parents=True, exist_ok=True)
    paths.checkpoint_file(_host()).write_text(content, encoding="utf-8")
    assert daemon.read_checkpoint_session(_host()) is None


def test_no_checkpoint_has_no_session():
    assert daemon.read_checkpoint_session(_host()) is None


def test_recovery_reads_a_checkpoint_that_carries_a_session_state(cfg):
    paths.checkpoint_file(_host()).parent.mkdir(parents=True, exist_ok=True)
    paths.checkpoint_file(_host()).write_text(
        json.dumps({"ts": _T0.isoformat(), "pid": 1, "session": "locked"}), encoding="utf-8"
    )
    _run(cfg, _ManualClock(_T0 + timedelta(hours=1)), max_ticks=0)
    assert _summary()[0] == ("collector_stop", "unclean", _T0, "inferred")


class TestProbeReconciler:
    def test_agreement_returns_nothing(self):
        reconciler = daemon.ProbeReconciler(_UNLOCKED)
        assert reconciler.observe(_UNLOCKED, _T0) is None
        assert reconciler.known is _UNLOCKED

    def test_two_consecutive_disagreements_confirm_at_the_first(self):
        reconciler = daemon.ProbeReconciler(_UNLOCKED)
        assert reconciler.observe(_LOCKED, _T0) is None
        assert reconciler.observe(_LOCKED, _T0 + timedelta(seconds=30)) == (_LOCKED, _T0)
        assert reconciler.known is _LOCKED
        assert reconciler.observe(_LOCKED, _T0 + timedelta(seconds=60)) is None

    def test_an_unknown_known_state_is_settled_by_two_readings(self):
        reconciler = daemon.ProbeReconciler(None)
        assert reconciler.observe(_LOCKED, _T0) is None
        assert reconciler.observe(_LOCKED, _T0 + timedelta(seconds=30)) == (_LOCKED, _T0)

    def test_an_unknown_reading_breaks_the_run(self):
        reconciler = daemon.ProbeReconciler(_UNLOCKED)
        reconciler.observe(_LOCKED, _T0)
        assert reconciler.observe(None, _T0 + timedelta(seconds=30)) is None
        assert reconciler.observe(_LOCKED, _T0 + timedelta(seconds=60)) is None
        assert reconciler.known is _UNLOCKED

    def test_a_heard_transition_drops_the_pending_reading(self):
        reconciler = daemon.ProbeReconciler(_UNLOCKED)
        reconciler.observe(_LOCKED, _T0)
        reconciler.heard(_LOCKED)
        assert reconciler.observe(_LOCKED, _T0 + timedelta(seconds=30)) is None
        assert reconciler.known is _LOCKED
