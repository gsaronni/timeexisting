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

    reason = daemon.run_collector(clock, sleep, cfg, max_ticks=100, on_start=captured.append)

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

    def on_start(_shutdown: daemon.Shutdown) -> None:
        seen.append((len(_ledger()), paths.checkpoint_file(_host()).exists()))

    _run(cfg, _ManualClock(_T0), max_ticks=0, on_start=on_start)
    assert seen == [(1, True)]


def test_shutdown_is_idempotent(cfg):
    clock = _ManualClock(_T0)
    results: list[bool] = []

    def on_start(shutdown: daemon.Shutdown) -> None:
        results.append(shutdown(daemon.StopReason.SESSION_END))
        results.append(shutdown(daemon.StopReason.CONSOLE_CLOSE))

    reason = _run(cfg, clock, max_ticks=5, on_start=on_start)

    assert results == [True, False]
    assert reason is daemon.StopReason.SESSION_END
    assert clock.sleeps == 0
    assert [reason for _, reason, _, _ in _summary()] == ["launch", "session_end"]


def test_concurrent_shutdowns_write_one_stop(cfg):
    results: list[bool] = []

    def on_start(shutdown: daemon.Shutdown) -> None:
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
