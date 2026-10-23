"""What the collector knows of the presence model (roadmap section 6) while it runs: the session state at a given time, and whether the machine is now awake on an unlocked session with no evidence yet. The input evidence rules (phase 3 step 7) read it; replay stays the only judge of the timeline.

It mirrors replay's rules for the facts it needs. `lock` and `suspend` clear evidence, `unlock` sets it, `input` sets it only while awake and unlocked, and a run's start sets the machine awake with the session the startup probe read. A `boot` follows replay's boot steps: a boot start sets the machine awake and the session locked, as at the sign-in screen, and a boot stop sets the machine off; neither is evidence. One rule is stricter than replay, which skips a boot without a kind: here such a boot sets the session unknown and leaves the machine as it was, so a reboot that cannot be classified still breaks the session carried from before it. Every boot applies wherever it falls, also in a gap replay does not reconstruct, so the tracker can lose evidence there but never invent it.

A `collector_stop` is looked through: the session at a time inside a gap is the last lock, unlock or boot before it. For a gap the logs do not cover, replay resets the session to unknown and drops an input written there, so this can lose evidence but never invent it.

Transitions are folded in time order, not arrival order, so a record backfilled from the event logs with a past time slots into place. At the same time, earlier arrivals come first.

Pure: no clock, no I/O.
"""

import bisect
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto

from timeexisting.ledger.events import Event, EventType, Source
from timeexisting.presence.backend import SessionState
from timeexisting.presence.eventlog import BootKind

_SESSION_OF = {EventType.LOCK: SessionState.LOCKED, EventType.UNLOCK: SessionState.UNLOCKED}
_MACHINE_EVENTS = frozenset({EventType.SUSPEND, EventType.RESUME})
_TRACKED = frozenset({*_SESSION_OF, *_MACHINE_EVENTS, EventType.INPUT, EventType.BOOT})


class _Machine(Enum):
    AWAKE = auto()
    ASLEEP = auto()
    # After a boot stop. Like asleep for the tracker: never awake, so never wanting input.
    OFF = auto()


class _Boot(Enum):
    START = auto()
    STOP = auto()
    # A `boot` whose `boot_kind` is missing or is neither `start` nor `stop`.
    KINDLESS = auto()


_BOOT_OF = {str(BootKind.START): _Boot.START, str(BootKind.STOP): _Boot.STOP}


def _boot(kind: str | None) -> _Boot:
    return _BOOT_OF.get(kind or "", _Boot.KINDLESS)


@dataclass(frozen=True, slots=True)
class _Entry:
    """One transition. `event` is `collector_start` for a run's start, which carries the session state the startup probe read. `boot` is the kind of a `boot`, `None` for any other event."""

    ts: datetime
    seq: int
    event: EventType
    session: SessionState | None = None
    boot: _Boot | None = None


def _order(entry: _Entry) -> tuple[datetime, int]:
    return (entry.ts, entry.seq)


def _by_time(event: Event) -> tuple[datetime, str]:
    return (event.ts, event.id)


def _sets_session(event: Event) -> bool:
    """A lock, an unlock, or a boot that is not a boot stop: each fixes the session from its time on."""
    if event.event is EventType.BOOT:
        return _boot(event.data.get("boot_kind")) is not _Boot.STOP
    return event.event in _SESSION_OF


def _sets_machine(event: Event) -> bool:
    """A suspend, a resume, or a boot start or stop: each fixes the machine from its time on."""
    if event.event is EventType.BOOT:
        return _boot(event.data.get("boot_kind")) is not _Boot.KINDLESS
    return event.event in _MACHINE_EVENTS


class Evidence:
    """The tracker. `events` are folded in `(ts, id)` order; `seed` picks the bounded slice of a ledger the collector needs."""

    def __init__(self, events: Iterable[Event] = ()) -> None:
        self._entries: list[_Entry] = []
        self._seq = 0
        self._since: datetime | None = None
        self._stale = True
        # The time of the last `win32` input in the ledger: a later one is written only when strictly newer.
        self.last_win32: datetime | None = None
        for event in sorted(events, key=_by_time):
            self.heard(event.ts, event.event, event.source, event.data.get("boot_kind"))

    @classmethod
    def seed(cls, events: Iterable[Event], anchor: datetime | None) -> Evidence:
        """A tracker from one host's ledger, bounded from the end. `anchor` is the System log cursor the run reads from: no record before it is evaluated again.

        The bound is the earlier of `anchor` and the last record that fixes the machine (a suspend, a resume, a boot start or stop), and the slice starts at the last record that fixes the session (a lock, an unlock, a boot start or a boot without a kind) at or before it. That one record fixes the session from there on, the last machine record fixes the machine, and every later transition is kept, so `session_at` at or after the bound and the current state match a fold of the whole ledger. The last `win32` input is taken from the whole ledger.
        """
        tracked = sorted((event for event in events if event.event in _TRACKED), key=_by_time)
        machine = [event.ts for event in tracked if _sets_machine(event)]
        marks = [mark for mark in (anchor, machine[-1] if machine else None) if mark is not None]
        kept = tracked
        if marks:
            bound = min(marks)
            before = [event.ts for event in tracked if _sets_session(event) and event.ts <= bound]
            begin = before[-1] if before else bound
            kept = [event for event in tracked if event.ts >= begin]
        tracker = cls(kept)
        win32 = [e.ts for e in tracked if e.event is EventType.INPUT and e.source is Source.WIN32]
        if win32:
            tracker.last_win32 = max(win32)
        return tracker

    def _add(self, entry: _Entry) -> None:
        bisect.insort(self._entries, entry, key=_order)
        self._stale = True

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def heard(self, ts: datetime, event: EventType, source: Source, boot_kind: str | None = None) -> None:
        """A transition written to the ledger. `boot_kind` is the `boot_kind` field of a `boot`, ignored for any other event. Events that say nothing about machine, session or input are ignored."""
        if event not in _TRACKED:
            return
        boot = _boot(boot_kind) if event is EventType.BOOT else None
        self._add(_Entry(ts, self._next(), event, boot=boot))
        if event is EventType.INPUT and source is Source.WIN32:
            self.last_win32 = ts if self.last_win32 is None else max(self.last_win32, ts)

    def started(self, ts: datetime, session: SessionState | None) -> None:
        """This run's start at `ts`: the machine is awake and the session is what the startup probe read, `None` when it could not tell. An unlocked session is evidenced, since the startup probe writes an `unlock` for it."""
        self._add(_Entry(ts, self._next(), EventType.COLLECTOR_START, session))

    def session_at(self, ts: datetime) -> SessionState | None:
        """The session at `ts`: from the last lock, unlock, boot or run start with a known session at or before it, or `None` when there is none. A boot start reads as locked, a boot without a kind as `None`, and a boot stop leaves the session as it was. A lock at exactly `ts` wins, so a wake that shares its time with a lock is on a locked session."""
        if any(entry.event is EventType.LOCK and entry.ts == ts for entry in self._entries):
            return SessionState.LOCKED
        state: SessionState | None = None
        for entry in self._entries:
            if entry.ts > ts:
                break
            if entry.event in _SESSION_OF:
                state = _SESSION_OF[entry.event]
            elif entry.event is EventType.COLLECTOR_START and entry.session is not None:
                state = entry.session
            elif entry.boot is _Boot.START:
                state = SessionState.LOCKED
            elif entry.boot is _Boot.KINDLESS:
                state = None
        return state

    @property
    def unevidenced_since(self) -> datetime | None:
        """When the current stretch of awake, unlocked and unevidenced began, or `None` when the state is anything else."""
        if self._stale:
            self._since = self._fold()
            self._stale = False
        return self._since

    def wants_input(self) -> bool:
        """Awake, unlocked and unevidenced now: the only state in which `GetLastInputInfo` is read."""
        return self.unevidenced_since is not None

    def _fold(self) -> datetime | None:
        machine: _Machine | None = None
        session: SessionState | None = None
        evidenced = False
        since: datetime | None = None
        for entry in self._entries:
            match entry.event:
                case EventType.COLLECTOR_START:
                    machine = _Machine.AWAKE
                    session = entry.session
                    evidenced = session is SessionState.UNLOCKED
                case EventType.LOCK:
                    session = SessionState.LOCKED
                    evidenced = False
                case EventType.UNLOCK:
                    session = SessionState.UNLOCKED
                    evidenced = True
                case EventType.SUSPEND:
                    machine = _Machine.ASLEEP
                    evidenced = False
                case EventType.RESUME:
                    machine = _Machine.AWAKE
                case EventType.INPUT:
                    if machine is _Machine.AWAKE and session is SessionState.UNLOCKED:
                        evidenced = True
                case EventType.BOOT:
                    evidenced = False
                    match entry.boot:
                        case _Boot.START:
                            machine = _Machine.AWAKE
                            session = SessionState.LOCKED
                        case _Boot.STOP:
                            machine = _Machine.OFF
                        case _:
                            session = None
            waiting = machine is _Machine.AWAKE and session is SessionState.UNLOCKED and not evidenced
            if not waiting:
                since = None
            elif since is None:
                since = entry.ts
        return since
