"""Event log reconciliation in the collector (roadmap section 8, phase 3 step 6a): pending sleep gaps, the per-tick System and Winlogon/Operational reads behind their cursors, the backfill of a gap between runs and its coverage note.

The event log is `backend.EventLog` over fake seams answering hand-written XML built from `fixtures/eventlog/` with invented values, so the real parser and `eventlog.boundaries()` run. Each record becomes visible to a query only from the instant it was written, which is how a record that lags the wake is modelled. No test touches the real event log, session or clock.
"""

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from timeexisting import paths
from timeexisting.collector import daemon
from timeexisting.collector.reconcile import PENDING_WINDOW, PendingGap, ReadState, Reconciler
from timeexisting.ledger import store
from timeexisting.ledger.events import Confidence, Event, EventType, Source
from timeexisting.ledger.replay import DELIBERATE_REASONS, Coverage, IntervalType, read_coverage, replay
from timeexisting.presence.backend import ChannelCheck, EventLog, NoEventLog
from timeexisting.presence.eventlog import (
    SYSTEM_CHANNEL,
    WAKE_REPORT_LAG,
    WINLOGON_CHANNEL,
    EventLogUnavailable,
    record_time,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "eventlog"
_USER = "S-1-5-21-1000000001-1000000002-1000000003-1001"
_T0 = datetime(2026, 9, 17, 6, 0, 0, tzinfo=UTC)
_TICK = timedelta(seconds=30)
_LONG_AGO = datetime(2026, 8, 1, tzinfo=UTC)

_STANDBY_506 = "System Kernel-Power 506 reason 12"
_WAKE_507 = "System Kernel-Power 507 reason 32"
_LOCK = "Winlogon/Operational 811 Sens 4"
_UNLOCK = "Winlogon/Operational 811 Sens 5"


# Records.


def _system_time(ts: datetime) -> str:
    return f"{ts.astimezone(UTC):%Y-%m-%dT%H:%M:%S.%f}0Z"


def _fixture(name: str, ts: datetime, event_id: int | None = None, **data: object) -> str:
    xml = (_FIXTURES / name).read_text(encoding="utf-8")
    xml, count = re.subn(r"SystemTime='[^']*'", f"SystemTime='{_system_time(ts)}'", xml)
    assert count == 1
    if event_id is not None:
        xml, count = re.subn(r"(<EventID[^>]*>)\d+(</EventID>)", rf"\g<1>{event_id}\g<2>", xml)
        assert count == 1
    for key, value in data.items():
        text = _system_time(value) if isinstance(value, datetime) else str(value)
        xml, count = re.subn(rf"(<Data Name='{key}'>)[^<]*(</Data>)", rf"\g<1>{text}\g<2>", xml)
        assert count == 1, key
    return xml


def _506(ts: datetime, reason: int = 12) -> str:
    return _fixture("kernel-power-506.xml", ts, Reason=reason)


def _507(ts: datetime, reason: int = 32) -> str:
    return _fixture("kernel-power-507.xml", ts, Reason=reason)


def _42(ts: datetime) -> str:
    return _fixture("kernel-power-42.xml", ts)


def _107(ts: datetime) -> str:
    return _fixture("kernel-power-42.xml", ts, event_id=107)


def _wake_report(ts: datetime, sleep: datetime, wake: datetime) -> str:
    return _fixture("power-troubleshooter-1.xml", ts, SleepTime=sleep, WakeTime=wake)


def _clock_change(ts: datetime, old: datetime, new: datetime) -> str:
    return _fixture("kernel-general-1.xml", ts, OldTime=old, NewTime=new, Reason=2)


def _boot_start(ts: datetime) -> str:
    return _fixture("eventlog-6005.xml", ts)


def _sens(code: int, ts: datetime) -> str:
    return _fixture("winlogon-811.xml", ts, Event=code)


class _Log:
    """The four event log seams over two channels of XML. A record is visible from `visible` (its own time by default) on the fake clock; a query honours the `TimeCreated` filter in its XPath the way `EvtQuery` does, and every query is recorded."""

    def __init__(self, clock) -> None:
        self.clock = clock
        self.channels: dict[str, list[tuple[datetime, str]]] = {SYSTEM_CHANNEL: [], WINLOGON_CHANNEL: []}
        self.unreadable: set[str] = set()
        self.queries: list[tuple[str, str]] = []

    def system(self, xml: str, visible: datetime | None = None) -> None:
        self.channels[SYSTEM_CHANNEL].append((visible or record_time(xml), xml))

    def winlogon(self, xml: str, visible: datetime | None = None) -> None:
        self.channels[WINLOGON_CHANNEL].append((visible or record_time(xml), xml))

    def _visible(self, channel: str) -> list[str]:
        if channel in self.unreadable:
            raise EventLogUnavailable(f"{channel}: EvtQuery failed, 5 Access is denied.")
        now = self.clock.now()
        return sorted((xml for visible, xml in self.channels[channel] if visible <= now), key=record_time)

    def query(self, channel: str, xpath: str) -> list[str]:
        self.queries.append((channel, xpath))
        records = self._visible(channel)
        match = re.search(r"SystemTime>='([^']+)'", xpath)
        if match is None:
            return records
        since = datetime.fromisoformat(match.group(1))
        return [xml for xml in records if record_time(xml) >= since]

    def first(self, channel: str, xpath: str) -> str | None:
        self.queries.append((channel, xpath))
        records = self._visible(channel)
        return records[0] if records else None

    def probe(self, channel: str) -> None:
        if channel in self.unreadable:
            raise EventLogUnavailable(f"{channel}: EvtQuery failed, 5 Access is denied.")

    def reader(self) -> EventLog:
        return EventLog(self.query, self.probe, lambda: _USER, self.first)

    def reads(self, channel: str) -> int:
        return sum(1 for queried, xpath in self.queries if queried == channel and "TimeCreated" in xpath)


class _Clock:
    """Moves only when the fake `sleep` moves it, by the tick plus any jump queued for that sleep. `seen` is the checkpoint as it stood at each sleep."""

    def __init__(self, start: datetime) -> None:
        self.current = start
        self.jumps: dict[int, timedelta] = {}
        self.sleeps = 0
        self.seen: list[dict] = []

    def now(self) -> datetime:
        return self.current

    def sleep(self, seconds: float) -> None:
        checkpoint = paths.checkpoint_file(_host())
        self.seen.append(json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {})
        self.sleeps += 1
        self.current += timedelta(seconds=seconds) + self.jumps.get(self.sleeps, timedelta())


def _host() -> str:
    return paths.current_host()


def _ledger() -> tuple[Event, ...]:
    return store.read_shard(store.shard_path(_host())).events


def _lines(*kinds: EventType) -> list[tuple[str, str, datetime, str, str]]:
    wanted = set(kinds) or {
        EventType.SUSPEND,
        EventType.RESUME,
        EventType.LOCK,
        EventType.UNLOCK,
        EventType.BOOT,
    }
    return [
        (str(e.event), str(e.source), e.ts, str(e.confidence), e.data.get("detail", ""))
        for e in _ledger()
        if e.event in wanted
    ]


def _coverage_notes() -> list[Event]:
    return [e for e in _ledger() if e.event is EventType.NOTE and e.data.get("kind") == "coverage"]


def _put(event: EventType, ts: datetime, source: Source = Source.COLLECTOR, **data: str) -> None:
    store.append(
        Event.new(
            ts=ts,
            host=_host(),
            profile="work",
            event=event,
            source=source,
            confidence=Confidence.INFERRED if data.get("reason") == "unclean" else Confidence.OBSERVED,
            data=data or None,
        )
    )


def _checkpoint(ts: datetime, **fields: object) -> None:
    record = {"ts": ts.isoformat(), "pid": 1, **fields}
    paths.checkpoint_file(_host()).write_text(json.dumps(record), encoding="utf-8")


def _run(cfg, clock: _Clock, log: _Log | None, ticks: int, **kwargs) -> daemon.StopReason:
    return daemon.run_collector(
        clock, clock.sleep, cfg, max_ticks=ticks, event_log=None if log is None else log.reader(), **kwargs
    )


def _tick(n: int, start: datetime = _T0) -> datetime:
    return start + n * _TICK


# A clock jump: pending, resolved against the System log, or written from the clock.


def test_a_clock_jump_writes_no_stop_and_start_and_holds_a_pending_gap(cfg):
    clock = _Clock(_T0)
    clock.jumps[3] = timedelta(minutes=10)
    _run(cfg, clock, _Log(clock), 4)

    woke = _tick(3) + timedelta(minutes=10)
    assert [str(e.event) for e in _ledger() if e.source is Source.COLLECTOR] == [
        "collector_start",
        "collector_stop",
    ]
    # The checkpoint at the fourth sleep, written by the tick that found the jump.
    assert clock.seen[3]["pending"] == [{"start": _tick(2).isoformat(), "wake": woke.isoformat()}]


def test_a_record_that_arrives_on_the_third_retry_writes_the_eventlog_pair_and_no_clock_pair(cfg):
    clock = _Clock(_T0)
    clock.jumps[3] = timedelta(minutes=10)
    woke = _tick(3) + timedelta(minutes=10)
    log = _Log(clock)
    standby = _tick(2) + timedelta(seconds=5)
    wake = woke - timedelta(seconds=12)
    log.system(_506(standby))
    # Read at the jump, then one tick and two ticks later: visible only on the third read.
    log.system(_507(wake), visible=woke + 2 * _TICK)
    _run(cfg, clock, log, 8)

    assert _lines() == [
        ("suspend", "eventlog", standby, "observed", _STANDBY_506),
        ("resume", "eventlog", wake, "observed", _WAKE_507),
    ]
    # `seen[n]` is the checkpoint written by tick n: the jump at tick 3, the retries at ticks 4 and 5.
    assert clock.seen[4]["pending"] != [] and clock.seen[5]["pending"] == []


def test_a_record_that_never_arrives_writes_the_clock_pair_after_the_pending_window(cfg):
    clock = _Clock(_T0)
    clock.jumps[3] = timedelta(minutes=10)
    woke = _tick(3) + timedelta(minutes=10)
    _run(cfg, clock, _Log(clock), 10)

    assert _lines() == [
        ("suspend", "clock", _tick(2), "inferred", ""),
        ("resume", "clock", woke, "inferred", ""),
    ]
    written_at = woke + PENDING_WINDOW
    assert clock.seen[int(PENDING_WINDOW / _TICK) + 2]["pending"] != []
    assert all(
        record["pending"] == [] for record in clock.seen if datetime.fromisoformat(record["ts"]) >= written_at
    )


def test_a_record_later_than_the_window_still_comes_in_and_replay_keeps_its_time(cfg):
    clock = _Clock(_T0)
    clock.jumps[3] = timedelta(minutes=10)
    woke = _tick(3) + timedelta(minutes=10)
    log = _Log(clock)
    standby = _tick(2) + timedelta(seconds=5)
    wake = woke - timedelta(seconds=12)
    log.system(_506(standby))
    log.system(_507(wake), visible=woke + timedelta(minutes=3))
    _run(cfg, clock, log, 12)

    assert _lines() == [
        ("suspend", "eventlog", standby, "observed", _STANDBY_506),
        ("suspend", "clock", _tick(2), "inferred", ""),
        ("resume", "clock", woke, "inferred", ""),
        ("resume", "eventlog", wake, "observed", _WAKE_507),
    ]
    asleep = [i for i in replay(_ledger()).intervals if i.kind is IntervalType.ASLEEP]
    assert [(i.start, i.end) for i in asleep] == [(standby, wake)]


def test_an_unreadable_system_log_writes_the_clock_pair_on_the_jump_tick(cfg):
    clock = _Clock(_T0)
    clock.jumps[3] = timedelta(minutes=10)
    log = _Log(clock)
    log.unreadable.add(SYSTEM_CHANNEL)
    log.system(_506(_tick(2) + timedelta(seconds=5)))
    _run(cfg, clock, log, 3)

    assert _lines() == [
        ("suspend", "clock", _tick(2), "inferred", ""),
        ("resume", "clock", _tick(3) + timedelta(minutes=10), "inferred", ""),
    ]
    assert log.reads(SYSTEM_CHANNEL) == 0


def test_a_platform_without_an_event_log_writes_the_clock_pair_at_once(cfg):
    clock = _Clock(_T0)
    clock.jumps[2] = timedelta(minutes=10)
    daemon.run_collector(clock, clock.sleep, cfg, max_ticks=2, event_log=NoEventLog())
    assert [line[:2] for line in _lines()] == [("suspend", "clock"), ("resume", "clock")]


def test_a_clean_stop_writes_an_unresolved_pending_gap_from_the_clock_before_the_stop(cfg):
    clock = _Clock(_T0)
    clock.jumps[3] = timedelta(minutes=10)
    _run(cfg, clock, _Log(clock), 4)

    events = _ledger()
    assert [str(e.event) for e in events][-3:] == ["suspend", "resume", "collector_stop"]
    assert all(e.source is Source.CLOCK for e in events[-3:-1])


# Hibernation.


def _hibernation(
    log: _Log, asleep_at: datetime, hours: int, *, wake_report: bool
) -> tuple[datetime, datetime]:
    """A standby that the battery budget turns into a hibernation, with the records Windows writes on the way out. Returns the 506 time and the real wake."""
    standby = asleep_at
    hibernate = standby + timedelta(minutes=30)
    wake = hibernate + timedelta(hours=hours)
    log.system(_506(standby))
    log.system(_42(hibernate))
    # 107: written at the real wake, stamped with the clock frozen at hibernation entry.
    log.system(_107(hibernate + timedelta(seconds=10)), visible=wake + timedelta(seconds=1))
    log.system(_clock_change(wake, hibernate + timedelta(seconds=1), wake))
    # Windows passes through Modern Standby on the way out: not a sleep.
    log.system(_506(wake + timedelta(seconds=2), reason=20))
    log.system(_507(wake + timedelta(seconds=2.2), reason=15))
    if wake_report:
        log.system(_wake_report(wake + timedelta(seconds=4.5), hibernate, wake + timedelta(seconds=3)))
    return standby, wake


def test_a_hibernate_wake_resumes_at_the_wake_report_and_ignores_the_way_out_and_the_107(cfg):
    clock = _Clock(_T0)
    clock.jumps[3] = timedelta(hours=8)
    log = _Log(clock)
    standby, wake = _hibernation(log, _tick(2) + timedelta(seconds=5), 7, wake_report=True)
    assert wake < _tick(3) + timedelta(hours=8)
    _run(cfg, clock, log, 10)

    assert _lines() == [
        ("suspend", "eventlog", standby, "observed", _STANDBY_506),
        (
            "resume",
            "eventlog",
            wake + timedelta(seconds=3),
            "observed",
            "System Power-Troubleshooter 1 WakeTime",
        ),
    ]
    # The cursor stays on the suspend, so every re-read still sees the 42 that absorbs the way out.
    assert clock.seen[-1]["system_cursor"] == standby.isoformat()


def test_a_hibernate_wake_without_a_wake_report_resumes_at_the_clock_change_once_it_settles(cfg):
    clock = _Clock(_T0)
    clock.jumps[3] = timedelta(hours=8)
    log = _Log(clock)
    standby, wake = _hibernation(log, _tick(2) + timedelta(seconds=5), 7, wake_report=False)
    _run(cfg, clock, log, 10)

    assert _lines() == [
        ("suspend", "eventlog", standby, "observed", _STANDBY_506),
        ("resume", "eventlog", wake, "observed", "System Kernel-General 1 reason 2 NewTime"),
    ]
    assert WAKE_REPORT_LAG < PENDING_WINDOW


# The System log as the primary source: sleeps the clock never sees.


def test_a_506_before_the_last_tick_is_written(cfg):
    """The collector keeps ticking through the screen-off phase, so the standby begins before the last tick before the jump."""
    clock = _Clock(_T0)
    clock.jumps[6] = timedelta(minutes=20)
    log = _Log(clock)
    standby = _tick(1) + timedelta(seconds=3)
    wake = _tick(6) + timedelta(minutes=19)
    log.system(_506(standby))
    log.system(_507(wake))
    _run(cfg, clock, log, 8)
    assert [(kind, ts) for kind, _, ts, _, _ in _lines()] == [("suspend", standby), ("resume", wake)]


def test_a_standby_the_collector_ticks_through_is_written_without_a_clock_jump(cfg):
    clock = _Clock(_T0)
    log = _Log(clock)
    standby = _tick(2) + timedelta(seconds=3)
    wake = _tick(12) + timedelta(seconds=7)
    log.system(_506(standby))
    log.system(_507(wake))
    _run(cfg, clock, log, 16)

    assert _lines() == [
        ("suspend", "eventlog", standby, "observed", _STANDBY_506),
        ("resume", "eventlog", wake, "observed", _WAKE_507),
    ]
    assert all(record.get("pending", []) == [] for record in clock.seen)


def test_twenty_idle_ticks_append_nothing_although_every_tick_rereads_the_last_records(cfg):
    clock = _Clock(_T0)
    log = _Log(clock)
    log.system(_506(_T0 - timedelta(minutes=5)), visible=_T0)
    log.system(_507(_T0 - timedelta(minutes=1)), visible=_T0)
    log.winlogon(_sens(5, _T0 + timedelta(seconds=5)))
    _run(cfg, clock, log, 22)

    assert len(_lines()) == 1
    assert log.reads(SYSTEM_CHANNEL) == 23 and log.reads(WINLOGON_CHANNEL) == 23


def test_every_read_filters_on_provider_and_event_id_in_the_query(cfg):
    clock = _Clock(_T0)
    clock.jumps[2] = timedelta(minutes=10)
    log = _Log(clock)
    _put(EventType.COLLECTOR_START, _T0 - timedelta(hours=2), reason="launch")
    _put(EventType.COLLECTOR_STOP, _T0 - timedelta(hours=1), reason="session_end")
    _run(cfg, clock, log, 6)

    assert log.queries
    for channel, xpath in log.queries:
        assert ("EventID=506" in xpath) if channel == SYSTEM_CHANNEL else ("EventID=811" in xpath)


# Winlogon/Operational.


def test_winlogon_locks_and_unlocks_are_appended_at_their_logged_times_with_the_code(cfg):
    clock = _Clock(_T0)
    log = _Log(clock)
    lock = _tick(2) + timedelta(seconds=4.25)
    unlock = _tick(5) + timedelta(seconds=11.5)
    log.winlogon(_sens(4, lock))
    log.winlogon(_sens(5, unlock))
    log.winlogon(_sens(12, unlock + timedelta(seconds=1)))
    _run(cfg, clock, log, 8)

    assert _lines() == [
        ("lock", "eventlog", lock, "observed", _LOCK),
        ("unlock", "eventlog", unlock, "observed", _UNLOCK),
    ]
    # Code 12 (shell start) is neither lock nor unlock, so it moves nothing.
    assert clock.seen[-1]["winlogon_cursor"] == unlock.isoformat()


def test_a_lock_during_standby_lands_at_its_logged_time_and_the_late_wts_lock_pairs_with_it(cfg):
    clock = _Clock(_T0)
    clock.jumps[3] = timedelta(minutes=20)
    log = _Log(clock)
    standby = _tick(2) + timedelta(seconds=5)
    lock = standby + timedelta(minutes=5)
    wake = _tick(3) + timedelta(minutes=19)
    log.system(_506(standby))
    log.system(_507(wake))
    log.winlogon(_sens(4, lock))

    def on_start(_shutdown, inbox):
        original = clock.sleep

        def sleep(seconds: float) -> None:
            original(seconds)
            if clock.sleeps == 3:
                # WTS reports the lock only at the next wake, stamped at receipt.
                inbox.put(EventType.LOCK, Source.WTS)

        clock.sleep = sleep

    daemon.run_collector(
        clock,
        lambda seconds: clock.sleep(seconds),
        cfg,
        max_ticks=5,
        event_log=log.reader(),
        on_start=on_start,
    )
    woke = _tick(3) + timedelta(minutes=20)
    assert ("lock", "eventlog", lock, "observed", _LOCK) in _lines()
    assert ("lock", "wts", woke, "observed", "") in _lines()
    asleep = [(i.kind, i.start, i.end) for i in replay(_ledger()).intervals if i.start >= standby]
    assert asleep[0] == (IntervalType.ASLEEP, standby, wake)
    assert asleep[1][:2] == (IntervalType.AWAY, wake)


def test_a_wts_only_run_reads_no_winlogon_records(cfg):
    clock = _Clock(_T0)
    log = _Log(clock)
    log.unreadable.add(WINLOGON_CHANNEL)
    log.winlogon(_sens(4, _tick(1)))
    _run(cfg, clock, log, 4)
    assert log.reads(WINLOGON_CHANNEL) == 0
    assert _lines(EventType.LOCK, EventType.UNLOCK) == []


# Crashes.


def _crashed_run(cursor: datetime, crash: datetime, **fields: object) -> None:
    _put(EventType.COLLECTOR_START, cursor - timedelta(hours=1), reason="launch")
    _checkpoint(crash, system_cursor=cursor.isoformat(), winlogon_cursor=cursor.isoformat(), **fields)


@pytest.mark.parametrize("appended", [True, False])
def test_a_crash_between_the_winlogon_append_and_the_cursor_update_loses_nothing_and_repeats_nothing(
    cfg, appended
):
    cursor = _T0 - timedelta(hours=1)
    unlock = cursor + timedelta(minutes=10)
    _crashed_run(cursor, unlock + timedelta(seconds=20))
    if appended:
        _put(EventType.UNLOCK, unlock, Source.EVENTLOG, detail=_UNLOCK)
    clock = _Clock(_T0)
    log = _Log(clock)
    log.winlogon(_sens(5, unlock))
    _run(cfg, clock, log, 3)

    assert _lines(EventType.UNLOCK) == [("unlock", "eventlog", unlock, "observed", _UNLOCK)]
    unlocks = [e for e in replay(_ledger()).events if e.event is EventType.UNLOCK]
    assert len(unlocks) == 1


def test_a_record_repeated_in_the_ledger_counts_once_in_replay(cfg):
    unlock = _T0 - timedelta(minutes=50)
    _crashed_run(_T0 - timedelta(hours=1), unlock + timedelta(seconds=20))
    _put(EventType.UNLOCK, unlock, Source.EVENTLOG, detail=_UNLOCK)
    _put(EventType.UNLOCK, unlock, Source.EVENTLOG, detail=_UNLOCK)
    _run(cfg, _Clock(_T0), _Log(_Clock(_T0)), 1)
    run = [i for i in replay(_ledger()).intervals if i.start < _T0 - timedelta(minutes=40)]
    assert [(i.kind, i.start) for i in run if i.kind is IntervalType.PRESENT] == [
        (IntervalType.PRESENT, unlock)
    ]


@pytest.mark.parametrize("arrives", [True, False])
def test_a_pending_gap_survives_a_crash_and_is_settled_by_the_next_run(cfg, arrives):
    start, woke = _T0 - timedelta(hours=2), _T0 - timedelta(hours=1)
    standby, wake = start + timedelta(seconds=5), woke - timedelta(seconds=10)
    _crashed_run(
        start - timedelta(minutes=1),
        woke + timedelta(minutes=1),
        pending=[{"start": start.isoformat(), "wake": woke.isoformat()}],
    )
    clock = _Clock(_T0)
    log = _Log(clock)
    log.system(_506(standby))
    if arrives:
        log.system(_507(wake))
    _run(cfg, clock, log, 2)

    machine = _lines(EventType.SUSPEND, EventType.RESUME)
    if arrives:
        assert [line[:3] for line in machine] == [
            ("suspend", "eventlog", standby),
            ("resume", "eventlog", wake),
        ]
    else:
        assert ("suspend", "clock", start, "inferred", "") in machine
        assert ("resume", "clock", woke, "inferred", "") in machine
    assert clock.seen[0]["pending"] == []


# The gap between two runs: backfill and coverage.


def _gap_records(log: _Log, stop: datetime) -> list[tuple[str, str, datetime, str, str]]:
    standby, wake = stop + timedelta(minutes=5), stop + timedelta(minutes=50)
    lock, unlock = stop + timedelta(minutes=8), wake + timedelta(seconds=9)
    log.system(_506(standby))
    log.system(_507(wake))
    log.winlogon(_sens(4, lock))
    log.winlogon(_sens(5, unlock))
    return [
        ("suspend", "eventlog", standby, "observed", _STANDBY_506),
        ("lock", "eventlog", lock, "observed", _LOCK),
        ("resume", "eventlog", wake, "observed", _WAKE_507),
        ("unlock", "eventlog", unlock, "observed", _UNLOCK),
    ]


def _history(log: _Log) -> None:
    """Old enough records in both channels that each reaches back to any gap in these tests."""
    log.system(_506(_LONG_AGO), visible=_LONG_AGO)
    log.system(_507(_LONG_AGO + timedelta(minutes=1)))
    log.winlogon(_sens(5, _LONG_AGO))


def _covered(stop: datetime, start: datetime, **flags: bool) -> dict[str, str]:
    values = {
        "system_readable": True,
        "system_reaches_back": True,
        "winlogon_readable": True,
        "winlogon_reaches_back": True,
    } | flags
    return dict(Coverage(span_start=stop, span_end=start, **values).data())


def test_a_gap_after_an_unclean_stop_is_backfilled_with_one_coverage_note(cfg):
    stop = _T0 - timedelta(hours=1)
    _put(EventType.COLLECTOR_START, stop - timedelta(hours=1), reason="launch")
    _checkpoint(stop)
    clock = _Clock(_T0)
    log = _Log(clock)
    _history(log)
    expected = _gap_records(log, stop)
    _run(cfg, clock, log, 2)

    assert sorted(line for line in _lines() if line[2] > stop) == sorted(expected)
    (note,) = _coverage_notes()
    assert (note.ts, dict(note.data)) == (_T0, _covered(stop, _T0))
    assert read_coverage(note) == Coverage(stop, _T0, True, True, True, True)


def test_a_gap_after_a_session_end_is_backfilled_with_one_coverage_note(cfg):
    stop = _T0 - timedelta(hours=1)
    _put(EventType.COLLECTOR_START, stop - timedelta(hours=1), reason="launch")
    _put(EventType.COLLECTOR_STOP, stop, reason="session_end")
    clock = _Clock(_T0)
    log = _Log(clock)
    _history(log)
    expected = _gap_records(log, stop)
    _run(cfg, clock, log, 2)

    assert sorted(line for line in _lines() if line[2] > stop) == sorted(expected)
    assert [dict(note.data) for note in _coverage_notes()] == [_covered(stop, _T0)]


@pytest.mark.parametrize("reason", ["stop_file", "signal", "console_close", "max_ticks"])
def test_a_gap_after_a_deliberate_stop_gets_no_backfill_and_no_note(cfg, reason):
    stop = _T0 - timedelta(hours=1)
    _put(EventType.COLLECTOR_START, stop - timedelta(hours=1), reason="launch")
    _put(EventType.COLLECTOR_STOP, stop, reason=reason)
    clock = _Clock(_T0)
    log = _Log(clock)
    _history(log)
    _gap_records(log, stop)
    _run(cfg, clock, log, 2)

    assert [line for line in _lines() if line[2] > stop] == []
    assert _coverage_notes() == []
    assert clock.seen[0]["system_cursor"] == _T0.isoformat()
    assert clock.seen[0]["winlogon_cursor"] == _T0.isoformat()


def test_a_deliberate_stop_whose_checkpoint_was_left_behind_drops_its_cursors_and_pending_gaps(cfg):
    stop = _T0 - timedelta(hours=1)
    _put(EventType.COLLECTOR_START, stop - timedelta(hours=1), reason="launch")
    _put(EventType.COLLECTOR_STOP, stop, reason="stop_file")
    _checkpoint(
        stop - timedelta(seconds=10),
        system_cursor=(stop - timedelta(hours=1)).isoformat(),
        pending=[
            {
                "start": (stop - timedelta(minutes=9)).isoformat(),
                "wake": (stop - timedelta(minutes=2)).isoformat(),
            }
        ],
    )
    clock = _Clock(_T0)
    _run(cfg, clock, _Log(clock), 1)
    assert _lines() == []
    assert (clock.seen[0]["system_cursor"], clock.seen[0]["pending"]) == (_T0.isoformat(), [])


@pytest.mark.parametrize(
    ("unreadable", "oldest_after_stop", "flags"),
    [
        ((WINLOGON_CHANNEL,), (), {"winlogon_readable": False, "winlogon_reaches_back": False}),
        ((SYSTEM_CHANNEL,), (), {"system_readable": False, "system_reaches_back": False}),
        ((), (SYSTEM_CHANNEL,), {"system_reaches_back": False}),
        ((), (WINLOGON_CHANNEL,), {"winlogon_reaches_back": False}),
    ],
)
def test_coverage_records_each_channel_unreadable_or_beyond_retention(
    cfg, unreadable, oldest_after_stop, flags
):
    stop = _T0 - timedelta(hours=1)
    _put(EventType.COLLECTOR_START, stop - timedelta(hours=1), reason="launch")
    _put(EventType.COLLECTOR_STOP, stop, reason="session_end")
    clock = _Clock(_T0)
    log = _Log(clock)
    log.unreadable.update(unreadable)
    for channel in (SYSTEM_CHANNEL, WINLOGON_CHANNEL):
        if channel not in oldest_after_stop:
            (log.system if channel == SYSTEM_CHANNEL else log.winlogon)(
                _506(_LONG_AGO) if channel == SYSTEM_CHANNEL else _sens(5, _LONG_AGO)
            )
    _gap_records(log, stop)
    _run(cfg, clock, log, 1)
    assert [dict(note.data) for note in _coverage_notes()] == [_covered(stop, _T0, **flags)]


@pytest.mark.parametrize(
    ("covered", "booted", "written"),
    [
        (False, timedelta(minutes=-20), True),
        (True, timedelta(minutes=-20), False),
        (False, timedelta(hours=-2), False),
    ],
)
def test_boot_time_is_written_only_inside_a_gap_the_logs_do_not_fully_cover(cfg, covered, booted, written):
    stop = _T0 - timedelta(hours=1)
    _put(EventType.COLLECTOR_START, stop - timedelta(hours=1), reason="launch")
    _put(EventType.COLLECTOR_STOP, stop, reason="session_end")
    clock = _Clock(_T0)
    log = _Log(clock)
    if covered:
        _history(log)
    else:
        log.unreadable.add(WINLOGON_CHANNEL)
    _run(cfg, clock, log, 1, boot_time=lambda: _T0 + booted)

    boots = [(e.ts, e.source, dict(e.data)) for e in _ledger() if e.event is EventType.BOOT]
    expected = [(_T0 + booted, Source.COLLECTOR, {"detail": "boot_time", "boot_kind": "start"})]
    assert boots == (expected if written else [])


def test_a_boot_record_in_the_gap_carries_its_kind_as_a_field(cfg):
    stop = _T0 - timedelta(hours=1)
    _put(EventType.COLLECTOR_START, stop - timedelta(hours=1), reason="launch")
    _put(EventType.COLLECTOR_STOP, stop, reason="session_end")
    clock = _Clock(_T0)
    log = _Log(clock)
    booted = stop + timedelta(minutes=2)
    log.system(_boot_start(booted))
    _run(cfg, clock, log, 1)
    (boot,) = [e for e in _ledger() if e.event is EventType.BOOT]
    assert (boot.ts, boot.source, dict(boot.data)) == (
        booted,
        Source.EVENTLOG,
        {"detail": "System EventLog 6005", "boot_kind": "start"},
    )


# The no-repeat key set.


def test_a_cursor_in_yesterdays_records_rereads_them_today_and_appends_nothing(cfg):
    """Shards are one per host, not per day: yesterday's records sit in the same shard the run reads its keys from."""
    local = ZoneInfo("Europe/Copenhagen")
    standby = datetime(2026, 9, 16, 23, 50, tzinfo=local)
    wake = datetime(2026, 9, 16, 23, 58, tzinfo=local)
    unlock = datetime(2026, 9, 16, 23, 59, tzinfo=local)
    today = datetime(2026, 9, 17, 0, 10, tzinfo=local)
    _put(EventType.COLLECTOR_START, standby - timedelta(hours=1), reason="launch")
    _put(EventType.SUSPEND, standby, Source.EVENTLOG, detail=_STANDBY_506)
    _put(EventType.RESUME, wake, Source.EVENTLOG, detail=_WAKE_507)
    _put(EventType.UNLOCK, unlock, Source.EVENTLOG, detail=_UNLOCK)
    _checkpoint(
        unlock + timedelta(seconds=20), system_cursor=standby.isoformat(), winlogon_cursor=unlock.isoformat()
    )
    clock = _Clock(today)
    log = _Log(clock)
    log.system(_506(standby))
    log.system(_507(wake))
    log.winlogon(_sens(5, unlock))
    before = len(_lines())
    _run(cfg, clock, log, 4)

    assert len(_lines()) == before == 3
    assert log.reads(SYSTEM_CHANNEL) == 5


def test_another_hosts_record_with_the_same_key_does_not_suppress_this_hosts(cfg):
    standby = _T0 + timedelta(seconds=5)
    store.append(
        Event.new(
            ts=standby,
            host="OTHER-HOST",
            profile="work",
            event=EventType.SUSPEND,
            source=Source.EVENTLOG,
            data={"detail": _STANDBY_506},
        )
    )
    clock = _Clock(_T0)
    log = _Log(clock)
    log.system(_506(standby))
    _run(cfg, clock, log, 2)
    assert _lines() == [("suspend", "eventlog", standby, "observed", _STANDBY_506)]


def test_the_key_set_drops_keys_before_the_cursor_as_it_advances():
    clock = _Clock(_T0)
    log = _Log(clock)
    appended: list[tuple] = []
    reconciler = Reconciler(
        log.reader(),
        ChannelCheck(system=True, winlogon=True),
        ReadState(),
        _T0,
        _host(),
        (),
        lambda ts, event, *_rest, **_data: appended.append((ts, event)),
    )
    for n in range(50):
        sleep = _T0 + n * timedelta(hours=1)
        log.system(_506(sleep + timedelta(minutes=1)))
        log.system(_507(sleep + timedelta(minutes=30)))
        clock.current = sleep + timedelta(minutes=40)
        reconciler.tick(clock.current)
    assert len(appended) == 100
    # Private by design: only what one read can return again is held.
    assert len(reconciler._system_keys) <= 2


# The checkpoint.


def test_a_checkpoint_from_before_step_6_reads_as_an_empty_state():
    assert ReadState.from_record({"ts": _T0.isoformat(), "pid": 1, "session": "locked"}) == ReadState()


@pytest.mark.parametrize(
    "pending",
    [
        [{"start": "yesterday", "wake": _T0.isoformat()}],
        [{"start": _T0.isoformat(), "wake": (_T0 - timedelta(minutes=1)).isoformat()}],
        [{"start": "2026-09-17T06:00:00", "wake": _T0.isoformat()}],
        ["not a gap"],
        "not a list",
    ],
)
def test_an_unreadable_pending_gap_is_dropped_never_guessed(pending):
    assert ReadState.from_record({"pending": pending}).pending == []


def test_the_read_state_round_trips_through_the_checkpoint():
    state = ReadState(_T0, _T0 + timedelta(minutes=1), [PendingGap(_T0, _T0 + timedelta(hours=1))])
    daemon.write_checkpoint(_host(), _T0, None, state)
    assert daemon.read_checkpoint_reads(_host()) == state


# What the collector's ledger looks like through replay.


def test_the_collector_ledger_through_replay(cfg):
    """A run with a standby and a Winlogon lock and unlock, a crash, a gap with a backfilled lock, and a run that starts on a locked session. In-run sleeps are `asleep` at the logged times. Both logs cover the gap, so it is reconstructed: presence carries through the crash to the backfilled lock, every interval inside is `inferred`, there is no `downtime`, and the next run opens `away`."""
    clock = _Clock(_T0)
    log = _Log(clock)
    _history(log)
    unlock = _T0 + timedelta(seconds=2)
    lock = _tick(4) + timedelta(seconds=1)
    standby = _tick(6) + timedelta(seconds=3)
    wake = _tick(20) + timedelta(seconds=4)
    unlock_again = wake + timedelta(seconds=9)
    log.winlogon(_sens(5, unlock))
    log.winlogon(_sens(4, lock))
    log.system(_506(standby))
    log.system(_507(wake))
    log.winlogon(_sens(5, unlock_again))
    crash = _tick(30)

    def crashing(seconds: float) -> None:
        if clock.sleeps == 30:
            raise RuntimeError("killed")
        clock.sleep(seconds)

    with pytest.raises(RuntimeError):
        daemon.run_collector(clock, crashing, cfg, max_ticks=40, event_log=log.reader())
    second_lock = crash + timedelta(minutes=3)
    log.winlogon(_sens(4, second_lock))
    restart = crash + timedelta(minutes=30)
    clock.current = restart
    clock.sleeps = 0
    _run(cfg, clock, log, 2)

    shape = [(i.kind, i.start, i.end, str(i.confidence)) for i in replay(_ledger()).intervals]
    assert shape == [
        (IntervalType.UNEVIDENCED, _T0, unlock, "observed"),
        (IntervalType.PRESENT, unlock, lock, "observed"),
        (IntervalType.AWAY, lock, standby, "observed"),
        (IntervalType.ASLEEP, standby, wake, "observed"),
        (IntervalType.AWAY, wake, unlock_again, "observed"),
        (IntervalType.PRESENT, unlock_again, crash, "observed"),
        (IntervalType.PRESENT, crash, second_lock, "inferred"),
        (IntervalType.AWAY, second_lock, restart, "inferred"),
        (IntervalType.AWAY, restart, restart + 2 * _TICK, "observed"),
        (IntervalType.DOWNTIME, restart + 2 * _TICK, None, "observed"),
    ]


def test_a_reboot_both_logs_cover_is_off_then_away_until_the_logon(cfg):
    """Live check 6 in miniature: a `session_end` stop, then logoff, shutdown, boot and logon backfilled from both logs."""
    stop = _T0 - timedelta(hours=1)
    unlock = stop - timedelta(minutes=30)
    _put(EventType.COLLECTOR_START, stop - timedelta(hours=1), reason="launch")
    _put(EventType.UNLOCK, unlock, Source.EVENTLOG, detail=_UNLOCK)
    _put(EventType.COLLECTOR_STOP, stop, reason="session_end")
    clock = _Clock(_T0)
    log = _Log(clock)
    _history(log)
    logoff, shutdown = stop + timedelta(seconds=10), stop + timedelta(seconds=20)
    booted, logon = stop + timedelta(minutes=2), stop + timedelta(minutes=3)
    log.winlogon(_sens(3, logoff))
    log.system(_fixture("eventlog-6006.xml", shutdown))
    log.system(_boot_start(booted))
    log.winlogon(_sens(2, logon))
    _run(cfg, clock, log, 1)

    shape = [(i.kind, i.start, i.end, str(i.confidence)) for i in replay(_ledger()).intervals]
    assert shape[1:] == [
        (IntervalType.PRESENT, unlock, stop, "observed"),
        (IntervalType.PRESENT, stop, logoff, "inferred"),
        (IntervalType.AWAY, logoff, shutdown, "inferred"),
        (IntervalType.OFF, shutdown, booted, "inferred"),
        (IntervalType.AWAY, booted, logon, "inferred"),
        (IntervalType.PRESENT, logon, _T0, "inferred"),
        (IntervalType.PRESENT, _T0, _tick(1), "observed"),
        (IntervalType.DOWNTIME, _tick(1), None, "observed"),
    ]


def test_an_uncovered_gap_with_the_boot_time_line_is_off_up_to_the_boot_then_away(cfg):
    stop = _T0 - timedelta(hours=1)
    _put(EventType.COLLECTOR_START, stop - timedelta(hours=1), reason="launch")
    _put(EventType.COLLECTOR_STOP, stop, reason="session_end")
    clock = _Clock(_T0)
    log = _Log(clock)
    log.unreadable.add(WINLOGON_CHANNEL)
    booted = _T0 - timedelta(minutes=40)
    _run(cfg, clock, log, 1, boot_time=lambda: booted)

    shape = [(i.kind, i.start, i.end, str(i.confidence)) for i in replay(_ledger()).intervals]
    assert shape[1:3] == [
        (IntervalType.OFF, stop, booted, "inferred"),
        (IntervalType.AWAY, booted, _T0, "inferred"),
    ]


def test_replay_and_the_collector_agree_on_the_deliberate_stops():
    assert {str(reason) for reason in daemon.DELIBERATE_STOPS} == DELIBERATE_REASONS
