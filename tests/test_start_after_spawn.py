"""The viewer's start against the collector it spawns (phase 3 step 10, the 28 October finding).

On 28 October 2026 the viewer prompted for the start at 08:57:45, in the same second as the collector it had just spawned, although the event log already held the morning's logon and unlock. The viewer resolved the start as soon as the collector's lock appeared, and the lock is taken before the collector backfills the gap since its last run, so the viewer read a ledger that did not yet hold the records. Since then `spawn.ensure_running` waits, after the lock, for the checkpoint the collector writes after its first reconciliation.

The tests here run the real collector on a thread, with its first event log read gated so the interleaving is fixed, and then resolve the start the way `main` does: `spawn.ensure_running`, then `_resolve_start`. The first test lands the viewer's lock poll while that first read is in flight, which is the live shape, and failed before the wait; the second lands it after the first reconciliation and is the control that pins the expected start; the third pins which marker on disk means the backfill is done.
"""

import os
import threading
from datetime import UTC, datetime, time, timedelta

import pytest

from timeexisting import cli, paths
from timeexisting.collector import daemon, spawn
from timeexisting.domain.clock import FixedClock
from timeexisting.domain.day_start import DayStart, Provenance
from timeexisting.ledger import store
from timeexisting.ledger.events import Confidence, Event, EventType, Source
from timeexisting.presence.backend import ChannelCheck, SessionState
from timeexisting.presence.eventlog import BootKind, Boundary, SessionRecord

_WAIT = 5.0  # every wait in these tests is bounded, so a broken interleaving fails instead of hanging

# Wednesday 28 October 2026, Europe/Copenhagen (UTC+1 after the DST end on the 25th).
_LAUNCH = datetime(2026, 10, 28, 7, 57, 45, tzinfo=UTC)  # 08:57:45 local, the viewer and the collector
_PREVIOUS_START = datetime(2026, 10, 27, 6, 0, tzinfo=UTC)  # yesterday's run, 07:00 local
_CHECKPOINT = datetime(2026, 10, 27, 14, 52, 22, tzinfo=UTC)  # its last tick: the unclean stop
_LOGOFF = datetime(2026, 10, 27, 14, 52, 54, tzinfo=UTC)  # Winlogon 811 Sens 3
_SHUTDOWN = datetime(2026, 10, 27, 14, 53, 26, tzinfo=UTC)  # EventLog 6006
_BOOT = datetime(2026, 10, 27, 14, 54, 20, tzinfo=UTC)  # EventLog 6005
_UNLOCK = datetime(2026, 10, 28, 7, 25, 7, tzinfo=UTC)  # Winlogon 811 Sens 5: 08:25:07 local, today
_OLDEST = _PREVIOUS_START - timedelta(days=7)

_EXPECTED = DayStart(time(8, 25), Provenance.INFERRED)


class _GatedLog:
    """Both channels readable and reaching back past the gap. The first read of either channel sets `reached` and then blocks on `release`: the gap's records are in flight, as the real log read is while the viewer polls."""

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()
        self.boundaries = [
            Boundary(_SHUTDOWN, EventType.BOOT, "System EventLog 6006", boot_kind=BootKind.STOP),
            Boundary(_BOOT, EventType.BOOT, "System EventLog 6005", boot_kind=BootKind.START),
        ]
        self.records = [
            SessionRecord(_LOGOFF, EventType.LOCK, 3),
            SessionRecord(_UNLOCK, EventType.UNLOCK, 5),
        ]

    def _gate(self) -> None:
        self.reached.set()
        self.release.wait(_WAIT)

    def check(self) -> ChannelCheck:
        return ChannelCheck(system=True, winlogon=True)

    def system_boundaries(self, since: datetime, until: datetime) -> list[Boundary]:
        self._gate()
        return [b for b in self.boundaries if since <= b.time <= until]

    def session_records(self, since: datetime) -> list[SessionRecord]:
        self._gate()
        return [r for r in self.records if r.time >= since]

    def oldest(self, _channel: str) -> datetime | None:
        return _OLDEST


class _UnlockedSession:
    def state(self) -> SessionState | None:
        return SessionState.UNLOCKED

    def logon_time(self) -> datetime | None:
        return None


def _host() -> str:
    return paths.current_host()


def _previous_run() -> None:
    """Yesterday's run: started at 07:00 local on an unlocked session, and its checkpoint left behind at 15:52:22 local, so the spawned collector recovers an unclean stop there and backfills the gap."""
    for event, source, confidence, data in (
        (EventType.COLLECTOR_START, Source.COLLECTOR, Confidence.OBSERVED, {"reason": "launch"}),
        (EventType.UNLOCK, Source.PROBE, Confidence.INFERRED, None),
    ):
        store.append(
            Event.new(
                ts=_PREVIOUS_START,
                host=_host(),
                profile="work",
                event=event,
                source=source,
                confidence=confidence,
                data=data,
            )
        )
    daemon.write_checkpoint(_host(), _CHECKPOINT)


class _Collector:
    """The spawned collector: `daemon.run_collector` on a thread, over `log`, stopped by `stop`. `ready` is set from `on_start`, which the loop calls once its first reconciliation and the startup probe are on disk."""

    def __init__(self, cfg, log: _GatedLog) -> None:
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.failure: Exception | None = None
        self._thread = threading.Thread(target=self._run, args=(cfg, log), name="collector", daemon=True)

    def _run(self, cfg, log: _GatedLog) -> None:
        try:
            daemon.run_collector(
                FixedClock(_LAUNCH),
                lambda _seconds: self.stop.wait(0.01),
                cfg,
                stop_event=self.stop,
                on_start=lambda _shutdown, _inbox: self.ready.set(),
                event_log=log,
                session=_UnlockedSession(),
                boot_time=lambda: _BOOT,
            )
        except Exception as error:  # surfaced by `finish`
            self.failure = error

    def spawn(self) -> None:
        self._thread.start()

    def finish(self, log: _GatedLog) -> None:
        log.release.set()
        self.stop.set()
        self._thread.join(_WAIT)
        assert not self._thread.is_alive(), "the collector did not stop"
        assert self.failure is None, f"the collector failed: {self.failure!r}"


def _viewer_start(cfg, sleep) -> tuple[DayStart, list[str]]:
    """`main`'s order: make sure a collector runs, then resolve the start. `sleep` is the viewer's poll between lock checks. The prompt, when it appears, is answered with Enter and recorded in `prompts`."""
    ensured = spawn.ensure_running(spawner=lambda: None, sleep=sleep)
    assert ensured.owner is not None, "the spawned collector never took the lock"
    assert ensured.spawned
    prompts: list[str] = []
    args = cli.build_parser().parse_args([])
    start = cli._resolve_start(
        args,
        cfg,
        _LAUNCH.astimezone(cli.local_timezone()),
        read=lambda prompt: prompts.append(prompt) or "",
        write=lambda _text: None,
        suggest=lambda moment: cli._ledger_start(moment, cfg, boot_time=lambda: _BOOT),
        record=lambda _start, _detail: None,
    )
    return start, prompts


def test_the_viewer_takes_the_start_from_the_gap_the_spawned_collector_backfills(cfg):
    """28 October: the viewer's poll finds the lock while the collector's first event log read is in flight. The start must come from the unlock that read appends, not from a prompt over the ledger as it stood at the lock. Before the wait this failed with the prompt and a start of 09:00; now the viewer's next poll waits out the first reconciliation."""
    _previous_run()
    log = _GatedLog()
    collector = _Collector(cfg, log)
    polls = 0

    def sleep(_seconds: float) -> None:
        nonlocal polls
        polls += 1
        if polls == 1:
            collector.spawn()
            assert log.reached.wait(_WAIT), "the collector never reached its first log read"
            return
        # Every later poll, which only the settle wait makes: the records land and the first reconciliation completes.
        log.release.set()
        assert collector.ready.wait(_WAIT), "the collector never finished its first reconciliation"

    try:
        start, prompts = _viewer_start(cfg, sleep)
    finally:
        collector.finish(log)

    assert prompts == [], "the viewer prompted although the event log held the morning's unlock"
    assert start == _EXPECTED
    assert polls == 2, "one poll for the lock, one for the reconciled checkpoint"


def test_control_the_start_is_the_unlock_once_the_first_reconciliation_is_on_disk(cfg):
    """The same ledger, log and collector, with the viewer's first poll landing after the first reconciliation: the start is the backfilled unlock, inferred, without a prompt. This pins what the test above expects."""
    _previous_run()
    log = _GatedLog()
    collector = _Collector(cfg, log)

    def sleep(_seconds: float) -> None:
        if not collector.ready.is_set():
            collector.spawn()
            log.release.set()
            assert collector.ready.wait(_WAIT), "the collector never finished its first reconciliation"

    try:
        start, prompts = _viewer_start(cfg, sleep)
    finally:
        collector.finish(log)

    assert prompts == []
    assert start == _EXPECTED


@pytest.mark.parametrize("marker", ["coverage note", "first checkpoint"], ids=["note", "checkpoint"])
def test_the_markers_the_collector_writes_before_its_first_read_do_not_mean_the_gap_is_backfilled(
    cfg, marker
):
    """What the viewer could wait for: the coverage note is appended before the first read, and the checkpoint written right after `collector_start` holds neither the session nor the read cursors, so `reconciled_checkpoint` reads neither as done. Only the checkpoint written after the first reconciliation carries the cursors, and that is the marker `ensure_running` waits for."""
    _previous_run()
    log = _GatedLog()
    collector = _Collector(cfg, log)
    collector.spawn()
    try:
        assert log.reached.wait(_WAIT)
        if marker == "coverage note":
            assert [e.data.get("kind") for e in _events(EventType.NOTE)] == ["coverage"]
        else:
            reads = daemon.read_checkpoint_reads(_host())
            assert reads is not None
            assert reads.system_cursor is None and reads.winlogon_cursor is None
        assert daemon.reconciled_checkpoint(_host(), os.getpid()) is None
        assert [e.ts for e in _events(EventType.UNLOCK)] == [_PREVIOUS_START], (
            "the gap's unlock is not on disk yet"
        )

        log.release.set()
        assert collector.ready.wait(_WAIT)
        assert daemon.reconciled_checkpoint(_host(), os.getpid()) == _LAUNCH
        assert _UNLOCK in [e.ts for e in _events(EventType.UNLOCK)]
    finally:
        collector.finish(log)

    assert daemon.reconciled_checkpoint(_host(), os.getpid()) is None  # a clean stop deletes the checkpoint


def _events(kind: EventType) -> list[Event]:
    return [e for e in store.read_shard(store.shard_path(_host())).events if e.event is kind]
