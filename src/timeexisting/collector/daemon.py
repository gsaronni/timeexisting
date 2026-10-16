"""The collector loop (roadmap section 3): records the collector's own presence as `collector_start` and `collector_stop` transitions, and proves it is alive between them with a per-tick checkpoint file, never a ledger line.

`clock` and `sleep` are injected. Tick spacing and the suspend check are measured against the clock, never by counting sleeps, so a test runs hundreds of ticks instantly. The loop writes UTC timestamps, which is what makes midnight and a daylight-saving change non-events. The local day is read once, by the startup probe, to decide whether the session logon happened today.

At start the collector queries each event log channel once (`startup_check`). An unreadable Winlogon/Operational makes the run WTS-only, recorded as one `wts_only` note; an unreadable System log leaves the clock-jump path as the only source of sleep boundaries. Neither stops the collector.

Single writer (roadmap section 3). The session watcher's window thread never appends to the ledger: it puts each session or power transition, stamped at receipt, into the `Inbox`, and the tick loop drains it, so the tick loop's append is the only write path. The one exception is the shutdown, which drains the inbox itself before the stop, under the same mutex.

Event log reconciliation (roadmap section 8, `collector/reconcile.py`). Every tick reads the System log and Winlogon/Operational behind a cursor each and appends what they record. A clock jump is held as a pending gap until the System log resolves it, or written from the clock after two minutes. At start, a gap since the previous run that did not end deliberately gets a `coverage` note, and its records come in with the first read, because the cursors start at the gap's start; a deliberate stop's gap gets neither.

Session state (roadmap section 6). At start the session probe records the current state and writes the `unlock` that opens the run's presence when the session is unlocked. Each tick it is read again and compared with the last known state, which the checkpoint holds; a disagreement that persists for two consecutive ticks writes the missing transition, source `probe`, `inferred`.

Exit paths that write a clean `collector_stop`: the stop file, the stop event (set by a signal handler), `max_ticks`, and a session end or console close reported from another thread through the `Shutdown` handed to `on_start`. All of them go through that one idempotent `Shutdown`, so the first one wins and the rest do nothing. An unexpected exception writes no stop and leaves the checkpoint in place, so the next start records an inferred `unclean` stop at the last tick rather than a clean one that never happened.
"""

import json
import logging
import math
import os
import queue
import signal
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from enum import StrEnum
from zoneinfo import ZoneInfo

import psutil

from timeexisting import paths
from timeexisting.collector import lockfile
from timeexisting.collector.reconcile import ReadState, Reconciler, coverage
from timeexisting.config.models import Config
from timeexisting.domain.clock import Clock
from timeexisting.ledger import store
from timeexisting.ledger.events import Confidence, Event, EventType, NoteKind, Source, profile_for
from timeexisting.presence.backend import ChannelCheck, EventLogReader, SessionProbe, SessionState
from timeexisting.presence.eventlog import BootKind

logger = logging.getLogger(__name__)

ROLE = "collector"
SUSPEND_FACTOR = 2
_SLEEP_SLICE = 0.5

_SESSION_STATE_OF = {EventType.LOCK: SessionState.LOCKED, EventType.UNLOCK: SessionState.UNLOCKED}
_SESSION_EVENT_OF = {state: event for event, state in _SESSION_STATE_OF.items()}


class StartReason(StrEnum):
    LAUNCH = "launch"
    # Phase 2 only: a clock jump was written as a `suspended` stop and a `resumed` start. Kept so the values stay named; phase 3 writes a `suspend` and a `resume` instead.
    RESUMED = "resumed"


class StopReason(StrEnum):
    STOP_FILE = "stop_file"
    SIGNAL = "signal"
    MAX_TICKS = "max_ticks"
    UNCLEAN = "unclean"
    # Phase 2 only, see `StartReason.RESUMED`.
    SUSPENDED = "suspended"
    SESSION_END = "session_end"
    CONSOLE_CLOSE = "console_close"


# Stops the code treats as user intent (roadmap section 8): the gap after one gets no backfill and no reconstruction. `signal` and `console_close` can only reach a collector running in a terminal; `max_ticks` is test-only.
DELIBERATE_STOPS = frozenset(
    {StopReason.STOP_FILE, StopReason.SIGNAL, StopReason.CONSOLE_CLOSE, StopReason.MAX_TICKS}
)


def _append(
    ts: datetime,
    host: str,
    profile: str,
    event: EventType,
    source: Source,
    confidence: Confidence,
    data: Mapping[str, str] | None = None,
) -> None:
    store.append(
        Event.new(
            ts=ts,
            host=host,
            profile=profile,
            event=event,
            source=source,
            confidence=confidence,
            data=data,
        )
    )


def _write(
    ts: datetime, host: str, profile: str, event: EventType, reason: str, confidence: Confidence
) -> None:
    _append(ts, host, profile, event, Source.COLLECTOR, confidence, {"reason": reason})


@dataclass(frozen=True, slots=True)
class Transition:
    """A session or power transition the window thread received, stamped at receipt. `detail`, when set, goes into the event's `data`."""

    ts: datetime
    event: EventType
    source: Source
    detail: str | None = None


class Inbox:
    """The window thread's only way into the ledger (roadmap section 3, single writer). `put` stamps a transition with the clock at receipt and queues it; the tick loop drains the queue in arrival order and does the writing. Thread-safe."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._queue: queue.SimpleQueue[Transition] = queue.SimpleQueue()

    def put(self, event: EventType, source: Source, detail: str | None = None) -> None:
        self._queue.put(Transition(self._clock.now(), event, source, detail))

    def drain(self) -> list[Transition]:
        drained: list[Transition] = []
        while True:
            try:
                drained.append(self._queue.get_nowait())
            except queue.Empty:
                return drained


def _record(transition: Transition, host: str, profile: str) -> None:
    data = None if transition.detail is None else {"detail": transition.detail}
    _append(transition.ts, host, profile, transition.event, transition.source, Confidence.OBSERVED, data)


def write_checkpoint(
    host: str, ts: datetime, session: SessionState | None = None, reads: ReadState | None = None
) -> bool:
    """Atomically replace the checkpoint with `ts`, our pid and, when there are any, the last known `session` state and the event log `reads` (cursors and pending gaps): write a temp file beside it, fsync, `os.replace`. A `PermissionError` (Windows, while something else holds the file) is logged and reported as `False`; the next tick tries again."""
    target = paths.checkpoint_file(host)
    temporary = target.with_name(f"{target.name}.tmp")
    record: dict[str, object] = {"ts": ts.isoformat(), "pid": os.getpid()}
    if session is not None:
        record["session"] = str(session)
    if reads is not None:
        record.update(reads.record())
    payload = json.dumps(record).encode("utf-8")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    except PermissionError as error:
        logger.warning("checkpoint not written, retrying next tick: %s", error)
        return False
    return True


def read_checkpoint(host: str) -> datetime | None:
    """The checkpoint's `ts`, or `None` if there is no checkpoint. Raises `ValueError` if it exists but does not parse to an aware timestamp."""
    try:
        raw = paths.checkpoint_file(host).read_bytes()
    except FileNotFoundError:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
        ts = datetime.fromisoformat(record["ts"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError(f"unparseable checkpoint: {error}") from error
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"checkpoint ts is naive: {ts.isoformat()}")
    return ts


def read_checkpoint_session(host: str) -> SessionState | None:
    """The last known session state the checkpoint holds, or `None` when there is no checkpoint, it holds none, or it does not parse."""
    try:
        record = json.loads(paths.checkpoint_file(host).read_bytes().decode("utf-8"))
        return SessionState(record["session"])
    except OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError:
        return None


def read_checkpoint_reads(host: str) -> ReadState | None:
    """The event log cursors and pending gaps the checkpoint holds, or `None` when there is no checkpoint or it does not parse. A checkpoint without them gives an empty `ReadState`."""
    try:
        record = json.loads(paths.checkpoint_file(host).read_bytes().decode("utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        return None
    return ReadState.from_record(record) if isinstance(record, dict) else None


def delete_checkpoint(host: str) -> None:
    target = paths.checkpoint_file(host)
    target.unlink(missing_ok=True)
    target.with_name(f"{target.name}.tmp").unlink(missing_ok=True)


def _last_event(host: str) -> Event | None:
    events = store.read_shard(store.shard_path(host)).events
    return max(events, key=lambda event: (event.ts, event.id), default=None)


def recover(host: str, profile: str) -> ReadState | None:
    """Close the previous run if it ended without a clean stop, and return the event log cursors and pending gaps its checkpoint held. Runs after the lock is held and before `collector_start`.

    A leftover checkpoint means the previous run may not have stopped cleanly. If the host's last ledger event is already a `collector_stop` at or after the checkpoint's `ts`, it did, and only the checkpoint's deletion was lost. Otherwise an inferred `collector_stop` with reason `unclean` goes in at the checkpoint's `ts`. An unparseable checkpoint is deleted and nothing is written: nothing is guessed.
    """
    try:
        checkpoint = read_checkpoint(host)
    except ValueError as error:
        logger.warning("deleting %s without recovery: %s", paths.checkpoint_file(host), error)
        delete_checkpoint(host)
        return None
    if checkpoint is None:
        return None
    reads = read_checkpoint_reads(host)
    last = _last_event(host)
    if last is not None and last.event is EventType.COLLECTOR_STOP and last.ts >= checkpoint:
        logger.info(
            "previous run stopped cleanly at %s; only its checkpoint was left behind", last.ts.isoformat()
        )
    else:
        logger.warning("previous run ended uncleanly; inferring collector_stop at %s", checkpoint.isoformat())
        _write(checkpoint, host, profile, EventType.COLLECTOR_STOP, StopReason.UNCLEAN, Confidence.INFERRED)
    delete_checkpoint(host)
    return reads


def startup_check(event_log: EventLogReader, ts: datetime, host: str, profile: str) -> ChannelCheck | None:
    """Query each event log channel once (roadmap section 6). If Winlogon/Operational is unreadable, the run is WTS-only: logged, and one `wts_only` note written at `ts`. If the System log is unreadable, logged; sleep boundaries stay on the clock-jump path. Returns what was found, or `None` when the platform has no event log or the check itself failed. Never raises, never asks for elevation."""
    try:
        channels = event_log.check()
    except Exception:
        logger.exception("event log self-check failed; carrying on without it")
        return None
    if channels is None:
        logger.info("no event log on this platform: sleep boundaries from the clock-jump path only")
        return None
    if not channels.system:
        logger.warning(
            "System log unreadable: sleep boundaries fall back to the clock-jump path for this run"
        )
    if not channels.winlogon:
        logger.warning("Winlogon/Operational unreadable: WTS-only run, lock and unlock times are live only")
        store.append(
            Event.new(
                ts=ts,
                host=host,
                profile=profile,
                event=EventType.NOTE,
                source=Source.COLLECTOR,
                confidence=Confidence.OBSERVED,
                data={"kind": str(NoteKind.WTS_ONLY)},
            )
        )
    logger.info(
        "event log self-check: System %s, Winlogon/Operational %s", channels.system, channels.winlogon
    )
    return channels


def startup_probe(
    session: SessionProbe,
    ts: datetime,
    tz: tzinfo,
    host: str,
    profile: str,
    last_event: datetime | None = None,
) -> SessionState | None:
    """Record the session state at collector start (roadmap section 6) and return it. Unlocked, with a logon time on the local day of `ts` in `tz` and later than `last_event`: `unlock` source `logon` at the logon time, `observed`. Unlocked otherwise: `unlock` source `probe` at `ts`, `inferred`. Locked or unknown: nothing is written.

    `last_event` is the time of this host's last ledger event before this run started: the previous run's stop, or the stop recovered at its checkpoint as unclean. A logon at or before it belongs to a session an earlier run already covered; an unlock stamped there would sort before that stop, which closes it in replay, so the run would open unevidenced.
    """
    state = session.state()
    logger.info("startup probe: session %s", state or "unknown")
    if state is not SessionState.UNLOCKED:
        return state
    logon = session.logon_time()
    if (
        logon is not None
        and logon <= ts
        and (last_event is None or logon > last_event)
        and logon.astimezone(tz).date() == ts.astimezone(tz).date()
    ):
        _append(logon, host, profile, EventType.UNLOCK, Source.LOGON, Confidence.OBSERVED)
    else:
        _append(ts, host, profile, EventType.UNLOCK, Source.PROBE, Confidence.INFERRED)
    return state


class ProbeReconciler:
    """The two-tick rule (roadmap section 6). Holds the last known session state: `heard` sets it from a transition that was written, and `observe` compares a probe reading with it.

    A reading that disagrees is held as pending. The same reading on the next tick confirms it: `observe` returns the missing transition's state and the time of the first reading, the earliest the probe saw it, and the known state moves. A reading that agrees drops the pending one. One reading is never enough: a transition can be in flight between the probe and the session notification that reports it. An unknown reading (`None`) confirms nothing and drops the pending one, so two disagreeing readings count only when they are consecutive.
    """

    def __init__(self, known: SessionState | None) -> None:
        self.known = known
        self._pending: tuple[SessionState, datetime] | None = None

    def heard(self, state: SessionState) -> None:
        self.known = state
        self._pending = None

    def observe(self, reading: SessionState | None, ts: datetime) -> tuple[SessionState, datetime] | None:
        if reading is None or reading is self.known:
            self._pending = None
            return None
        if self._pending is not None and self._pending[0] is reading:
            confirmed = self._pending
            self.heard(reading)
            return confirmed
        self._pending = (reading, ts)
        return None


class Shutdown:
    """The one way a running collector stops cleanly: write whatever is still in the inbox, then `collector_stop` with the reason (fsynced by `store.append`), delete the checkpoint and the stop file, release the lock, and set the stop event so the loop exits.

    Idempotent and thread-safe. Logoff can deliver both a console control event and `WM_ENDSESSION`, on threads other than the loop's, so the first call does the work and every later call returns `False` having done nothing. The loop holds `mutex` while it writes a tick, so a shutdown never lands between a tick's ledger lines and no checkpoint is written after the shutdown deleted it.
    """

    def __init__(
        self,
        clock: Clock,
        host: str,
        profile: str,
        lock: lockfile.Lock,
        stop_event: threading.Event,
        inbox: Inbox | None = None,
    ) -> None:
        self.mutex = threading.RLock()
        self.reason: StopReason | None = None
        self.inbox = inbox or Inbox(clock)
        self._clock = clock
        self._host = host
        self._profile = profile
        self._lock = lock
        self._stop_event = stop_event
        # Writes what the run holds only in memory and the checkpoint, the pending sleep gaps, before the stop deletes the checkpoint. Set by the loop.
        self.flush: Callable[[], None] | None = None

    def __call__(self, reason: StopReason) -> bool:
        with self.mutex:
            if self.reason is not None:
                logger.info("shutdown (%s) ignored: already stopped (%s)", reason, self.reason)
                return False
            self.reason = reason
            for transition in self.inbox.drain():
                _record(transition, self._host, self._profile)
            if self.flush is not None:
                self.flush()
            _write(
                self._clock.now(),
                self._host,
                self._profile,
                EventType.COLLECTOR_STOP,
                reason,
                Confidence.OBSERVED,
            )
            delete_checkpoint(self._host)
            paths.stop_file().unlink(missing_ok=True)
            lockfile.release(self._lock)
            self._stop_event.set()
            logger.info("collector stopped: %s", reason)
            return True


def run_collector(
    clock: Clock,
    sleep: Callable[[float], object],
    cfg: Config,
    *,
    max_ticks: int | None = None,
    stop_event: threading.Event | None = None,
    on_start: Callable[[Shutdown, Inbox], object] | None = None,
    event_log: EventLogReader | None = None,
    session: SessionProbe | None = None,
    boot_time: Callable[[], datetime | None] | None = None,
) -> StopReason:
    """Acquire the lock, recover, write `collector_start`, then tick every `cfg.collector.tick` until the stop file appears, `stop_event` is set, `max_ticks` ticks have run, or another thread calls the `Shutdown`. Returns the stop reason.

    `event_log`, when given, gets the startup self-check (`startup_check`) right after `collector_start`, then the coverage note for a gap since a stop that was not deliberate, then the first read; it is read again every tick (`reconcile.Reconciler`). Without it a clock jump is written from the clock at once. `boot_time`, when given, is asked once, only for a gap the logs do not fully cover. `session`, when given, gets the startup probe (`startup_probe`) after that and is read again every tick (`ProbeReconciler`). `on_start` receives the `Shutdown` and the `Inbox` once all of that is on disk; the Windows session watcher hooks in there. Each tick drains the inbox into the ledger, then reads the event logs, then probes. Raises `lockfile.AlreadyRunning` or `lockfile.LockError` before writing anything if the lock cannot be taken.
    """
    stop_event = stop_event or threading.Event()
    host = paths.current_host()
    profile = profile_for(host, cfg)
    lock = lockfile.acquire(ROLE)
    shutdown = Shutdown(clock, host, profile, lock, stop_event, Inbox(clock))
    try:
        if paths.stop_file().exists():
            logger.warning("removing a stop file left over from an earlier stop request")
            paths.stop_file().unlink(missing_ok=True)
        leftover = recover(host, profile)
        reason = _loop(
            clock,
            sleep,
            cfg,
            host,
            profile,
            max_ticks,
            stop_event,
            shutdown,
            on_start,
            event_log,
            session,
            leftover,
            boot_time,
        )
        shutdown(reason)
        return shutdown.reason or reason
    except Exception:
        logger.exception("collector failed; checkpoint left for recovery")
        raise
    finally:
        lockfile.release(lock)


def _gap_start(events: Iterable[Event]) -> Event | None:
    """The stop that opens the gap before this run, when the gap is to be backfilled: this host's last collector event, if it is a `collector_stop` the code does not treat as user intent. `None` for a deliberate stop, a last event that is a start (where the previous run ended is unknown), and an empty shard."""
    collector = [e for e in events if e.event in {EventType.COLLECTOR_START, EventType.COLLECTOR_STOP}]
    last = max(collector, key=lambda event: (event.ts, event.id), default=None)
    if last is None or last.event is not EventType.COLLECTOR_STOP:
        return None
    if last.data.get("reason") in DELIBERATE_STOPS:
        return None
    return last


def _backfill_gap(
    stop: Event,
    start: datetime,
    event_log: EventLogReader,
    channels: ChannelCheck | None,
    boot_time: Callable[[], datetime | None] | None,
    host: str,
    profile: str,
) -> None:
    """The coverage note for the gap from `stop` to `start` (roadmap section 8), and a `boot` from `boot_time()` when the machine started inside a gap the logs do not fully cover. The gap's records come in with the first read, since the cursors start at the stop."""
    covered = coverage(event_log, channels, stop.ts, start)
    _append(start, host, profile, EventType.NOTE, Source.COLLECTOR, Confidence.OBSERVED, covered.data())
    logger.info("gap since %s: %s", stop.ts.isoformat(), dict(covered.data()))
    fully = (
        covered.system_readable
        and covered.system_reaches_back
        and covered.winlogon_readable
        and covered.winlogon_reaches_back
    )
    booted = boot_time() if boot_time is not None and not fully else None
    if booted is not None and stop.ts < booted < start:
        logger.info("machine booted at %s inside a gap the logs do not cover", booted.isoformat())
        _append(
            booted,
            host,
            profile,
            EventType.BOOT,
            Source.COLLECTOR,
            Confidence.OBSERVED,
            {"detail": "boot_time", "boot_kind": str(BootKind.START)},
        )


def _loop(
    clock: Clock,
    sleep: Callable[[float], object],
    cfg: Config,
    host: str,
    profile: str,
    max_ticks: int | None,
    stop_event: threading.Event,
    shutdown: Shutdown,
    on_start: Callable[[Shutdown, Inbox], object] | None,
    event_log: EventLogReader | None,
    session: SessionProbe | None,
    leftover: ReadState | None,
    boot_time: Callable[[], datetime | None] | None,
) -> StopReason:
    tick = cfg.collector.tick
    inbox = shutdown.inbox
    ledger = store.read_all().events
    own = [event for event in ledger if event.host == host]
    last = max(own, key=lambda event: (event.ts, event.id), default=None)
    gap = _gap_start(own)
    previous = clock.now()
    _write(previous, host, profile, EventType.COLLECTOR_START, StartReason.LAUNCH, Confidence.OBSERVED)
    write_checkpoint(host, previous)
    logger.info("collector started on %s, tick %s", host, tick)
    channels = startup_check(event_log, previous, host, profile) if event_log is not None else None
    if gap is None and leftover is not None and (leftover.pending or leftover.system_cursor is not None):
        logger.info(
            "the previous run stopped deliberately: its event log cursors and pending gaps are dropped"
        )
    reads = leftover if gap is not None and leftover is not None else ReadState()

    def append(
        ts: datetime,
        event: EventType,
        source: Source,
        confidence: Confidence,
        data: Mapping[str, str] | None = None,
    ) -> None:
        _append(ts, host, profile, event, source, confidence, data)

    reconciler = Reconciler(
        event_log, channels, reads, previous if gap is None else gap.ts, host, ledger, append
    )
    shutdown.flush = reconciler.flush
    if event_log is not None and gap is not None:
        _backfill_gap(gap, previous, event_log, channels, boot_time, host, profile)
    reconciler.tick(previous)
    probe = ProbeReconciler(None)
    if session is not None:
        probe.known = startup_probe(
            session,
            previous,
            ZoneInfo(cfg.display.timezone),
            host,
            profile,
            None if last is None else last.ts,
        )
    write_checkpoint(host, previous, probe.known, reconciler.state)
    if on_start is not None:
        on_start(shutdown, inbox)
    ticks = 0
    while True:
        if shutdown.reason is not None:
            return shutdown.reason
        if stop_event.is_set():
            return StopReason.SIGNAL
        if paths.stop_file().exists():
            return StopReason.STOP_FILE
        if max_ticks is not None and ticks >= max_ticks:
            return StopReason.MAX_TICKS
        sleep(tick.total_seconds())
        with shutdown.mutex:
            if shutdown.reason is not None:
                return shutdown.reason
            now = clock.now()
            ticks += 1
            if now < previous:
                logger.warning(
                    "clock moved backwards from %s to %s; not a suspend",
                    previous.isoformat(),
                    now.isoformat(),
                )
            elif now - previous > SUSPEND_FACTOR * tick:
                logger.info("clock jumped %s since the last tick: suspended", now - previous)
                reconciler.jump(previous, now)
            for transition in inbox.drain():
                _record(transition, host, profile)
                state = _SESSION_STATE_OF.get(transition.event)
                if state is not None:
                    probe.heard(state)
            reconciler.tick(now)
            if session is not None:
                missing = probe.observe(session.state(), now)
                if missing is not None:
                    state, seen = missing
                    logger.info("probe: session %s since %s with no notification", state, seen.isoformat())
                    _append(seen, host, profile, _SESSION_EVENT_OF[state], Source.PROBE, Confidence.INFERRED)
            write_checkpoint(host, now, probe.known, reconciler.state)
            previous = now


def system_boot_time() -> datetime | None:
    """When the machine last booted, aware UTC, from `psutil`; `None` when it cannot be read. The `boot_time` seam `te collect` passes to `run_collector`."""
    try:
        return datetime.fromtimestamp(psutil.boot_time(), UTC)
    except (OSError, psutil.Error) as error:
        logger.warning("boot time unreadable: %s", error)
        return None


def interruptible_sleep(stop_event: threading.Event) -> Callable[[float], None]:
    """A `sleep` for production: sleeps in short slices and returns early once `stop_event` is set, so Ctrl+C is honoured within half a second rather than at the end of a 30-second tick."""

    def sleep(seconds: float) -> None:
        for _ in range(math.ceil(seconds / _SLEEP_SLICE)):
            if stop_event.is_set():
                return
            time.sleep(_SLEEP_SLICE)

    return sleep


@contextmanager
def signal_handlers(stop_event: threading.Event) -> Iterator[None]:
    """Route SIGINT, and SIGBREAK on Windows or SIGTERM elsewhere, to `stop_event` for the duration of the block, then restore whatever was installed before. Main thread only."""
    names = ["SIGINT", "SIGBREAK"] if os.name == "nt" else ["SIGINT", "SIGTERM"]
    previous = {}
    for name in names:
        number = getattr(signal, name)
        previous[number] = signal.signal(number, lambda _signum, _frame: stop_event.set())
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
