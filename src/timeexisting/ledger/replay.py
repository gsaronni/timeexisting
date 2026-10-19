"""Replay the ledger into a timeline (roadmap sections 5, 6 and 8), per host: the typed intervals of the presence model, plus the collector's own runs and the gaps between them.

Pure. No clock, no I/O, no Rich: the caller reads the ledger and hands the events in. Nothing is classified here; a gap carries the reason its opening stop recorded and nothing more.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import Enum, StrEnum, auto

from timeexisting.ledger.events import Confidence, Event, EventType, NoteKind, Source
from timeexisting.presence.eventlog import BootKind

_BOOLEANS = {"true": True, "false": False}


class IntervalType(StrEnum):
    """The typed timeline's interval types (roadmap section 6)."""

    PRESENT = "present"
    AWAY = "away"
    ASLEEP = "asleep"
    OFF = "off"
    UNEVIDENCED = "unevidenced"
    DOWNTIME = "downtime"


@dataclass(frozen=True, slots=True)
class Interval:
    """One interval of the typed timeline on `host`. `end` is `None` while it is still open. `confidence` is `inferred` when the opening boundary was inferred, the closing one was an inferred transition rather than a stop, or the interval lies inside a gap replay reconstructed or folded; `source` is the opening boundary's."""

    kind: IntervalType
    start: datetime
    end: datetime | None
    host: str
    confidence: Confidence
    source: Source


@dataclass(frozen=True, slots=True)
class Presence:
    """A span the collector was running on `host`. `end` is the closing `collector_stop`'s `ts`, or `None` when no stop closes it: the running collector, one not yet recovered, or one followed by another start. `end_inferred` is true when that stop was inferred rather than observed."""

    start: datetime
    end: datetime | None
    host: str
    end_inferred: bool


@dataclass(frozen=True, slots=True)
class Gap:
    """The span from a `collector_stop` to the same host's next `collector_start`. Each edge carries its own confidence; `reason` is the stop's `data["reason"]`, or `None` if it recorded none."""

    start: datetime
    end: datetime
    host: str
    start_inferred: bool
    end_inferred: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class Timeline:
    """Every event in `(ts, id)` order, nothing collapsed, plus what is derived from them, each sorted by start then host: the typed `intervals`, and the collector's runs (`presences`) and the `gaps` between them. `presences` is collector liveness, not user presence; it feeds the start suggestion until the start resolution of roadmap section 4 replaces it."""

    events: tuple[Event, ...]
    intervals: tuple[Interval, ...]
    presences: tuple[Presence, ...]
    gaps: tuple[Gap, ...]


@dataclass(frozen=True, slots=True)
class Coverage:
    """What a structured `coverage` note says about a span in observation (roadmap section 5): whether the System log and Winlogon/Operational were readable, and whether each reaches back, meaning its oldest record is no later than `span_start`. Both stamps are aware UTC."""

    span_start: datetime
    span_end: datetime
    system_readable: bool
    system_reaches_back: bool
    winlogon_readable: bool
    winlogon_reaches_back: bool

    def data(self) -> Mapping[str, str]:
        """The note's `data` fields, as the collector writes them and `read_coverage` reads them back."""
        return {
            "kind": str(NoteKind.COVERAGE),
            "span_start": self.span_start.astimezone(UTC).isoformat(),
            "span_end": self.span_end.astimezone(UTC).isoformat(),
            "system_readable": _flag(self.system_readable),
            "system_reaches_back": _flag(self.system_reaches_back),
            "winlogon_readable": _flag(self.winlogon_readable),
            "winlogon_reaches_back": _flag(self.winlogon_reaches_back),
        }

    def covers(self, start: datetime, end: datetime) -> bool:
        """Whether both channels were readable and reach back, and the span holds `start` to `end`: the condition for reconstructing that gap (roadmap section 8). A partly covered gap counts as uncovered."""
        return (
            self.system_readable
            and self.system_reaches_back
            and self.winlogon_readable
            and self.winlogon_reaches_back
            and self.span_start <= start
            and end <= self.span_end
        )


def _flag(value: bool) -> str:
    return "true" if value else "false"


def _stamp(data: Mapping[str, str], key: str) -> datetime | None:
    try:
        value = datetime.fromisoformat(data[key])
    except KeyError, ValueError:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


def read_coverage(event: Event) -> Coverage | None:
    """The coverage a `note` of kind `coverage` records, from its `data` fields only, never its text. `None` for any other event, and for a coverage note with a field missing, a flag that is not exactly `true` or `false`, a naive or unparseable stamp, or a span that ends before it starts: replay then treats the span as uncovered."""
    if event.event is not EventType.NOTE or event.data.get("kind") != NoteKind.COVERAGE:
        return None
    data = event.data
    span_start = _stamp(data, "span_start")
    span_end = _stamp(data, "span_end")
    if span_start is None or span_end is None or span_end < span_start:
        return None
    flags: dict[str, bool] = {}
    for key in ("system_readable", "system_reaches_back", "winlogon_readable", "winlogon_reaches_back"):
        value = _BOOLEANS.get(data.get(key, ""))
        if value is None:
            return None
        flags[key] = value
    return Coverage(span_start=span_start, span_end=span_end, **flags)


class _Machine(Enum):
    AWAKE = auto()
    ASLEEP = auto()
    OFF = auto()
    UNKNOWN = auto()


class _Session(Enum):
    UNLOCKED = auto()
    LOCKED = auto()
    UNKNOWN = auto()


class _Step(Enum):
    START = auto()
    # Observation is lost: a stop whose gap is not reconstructed, or inside a reconstructed gap the last record before a boot that no shutdown record precedes.
    STOP = auto()
    # A stop whose gap is reconstructed from the event logs, or folded: the state before it carries through.
    CARRY = auto()
    # A stop whose gap the logs do not cover but which holds the collector's `boot_time()` line: the machine was off up to it.
    SHUTDOWN = auto()
    # Boot records inside a reconstructed gap, and the collector's `boot_time()` line.
    BOOT_STOP = auto()
    BOOT_START = auto()
    LOCK = auto()
    UNLOCK = auto()
    SUSPEND = auto()
    RESUME = auto()
    INPUT = auto()


_STEP_OF = {
    EventType.COLLECTOR_START: _Step.START,
    EventType.COLLECTOR_STOP: _Step.STOP,
    EventType.LOCK: _Step.LOCK,
    EventType.UNLOCK: _Step.UNLOCK,
    EventType.SUSPEND: _Step.SUSPEND,
    EventType.RESUME: _Step.RESUME,
    EventType.INPUT: _Step.INPUT,
}
_SESSION_EVENTS = {EventType.LOCK, EventType.UNLOCK}
_MACHINE_EVENTS = {EventType.SUSPEND, EventType.RESUME}
_COLLECTOR_EVENTS = {EventType.COLLECTOR_START, EventType.COLLECTOR_STOP, EventType.INPUT}
_BOOT_STEPS = {str(BootKind.START): _Step.BOOT_START, str(BootKind.STOP): _Step.BOOT_STOP}
# The records a reconstructed gap is read from.
_RECORD_EVENTS = {*_SESSION_EVENTS, *_MACHINE_EVENTS, EventType.INPUT, EventType.BOOT}
# A boundary where the collector stopped observing. It says nothing about the user, so an interval it closes keeps the confidence its other edge gives it.
_STOP_STEPS = {_Step.STOP, _Step.CARRY, _Step.SHUTDOWN}
# Phase 2 wrote a clock jump as a stop and start pair with these reasons (`StopReason.SUSPENDED`, `StartReason.RESUMED` in the collector); replay reads them as an inferred suspend and resume.
_LEGACY_SUSPENDED = "suspended"
_LEGACY_RESUMED = "resumed"
# The stops the collector treats as user intent (`DELIBERATE_STOPS` in the collector, roadmap section 8): their gap is never reconstructed, folded or turned into `off`.
DELIBERATE_REASONS = frozenset({"stop_file", "signal", "console_close", "max_ticks"})


@dataclass(frozen=True, slots=True)
class _Boundary:
    """One state change, at the time replay places it: the event's own time, or for a transition matched to an `eventlog` record, the record's."""

    ts: datetime
    id: str
    step: _Step
    source: Source
    confidence: Confidence


@dataclass(slots=True)
class _State:
    """The three facts of the presence model (machine, session, observation) plus whether presence is evidenced. `reconstructing` is true inside a gap replay reconstructs or folds: the collector is not observing, but the state carries through and is not downtime."""

    observing: bool = False
    reconstructing: bool = False
    machine: _Machine = _Machine.UNKNOWN
    session: _Session = _Session.UNKNOWN
    evidenced: bool = False

    def apply(self, step: _Step) -> None:
        match step:
            case _Step.START:
                self.observing = True
                self.reconstructing = False
                self.machine = _Machine.AWAKE
            case _Step.STOP:
                # Loss of observation closes presence and leaves machine and session unknown.
                self.observing = False
                self.reconstructing = False
                self.machine = _Machine.UNKNOWN
                self.session = _Session.UNKNOWN
                self.evidenced = False
            case _Step.CARRY:
                self.observing = False
                self.reconstructing = True
            case _Step.SHUTDOWN:
                self.observing = False
                self.reconstructing = True
                self.machine = _Machine.OFF
                self.evidenced = False
            case _Step.BOOT_STOP:
                self.machine = _Machine.OFF
                self.evidenced = False
            case _Step.BOOT_START:
                # A machine that has just started shows the sign-in screen: no session is unlocked until a logon. It also ends a stretch lost to a shutdown no record shows.
                self.reconstructing = True
                self.machine = _Machine.AWAKE
                self.session = _Session.LOCKED
                self.evidenced = False
            case _Step.LOCK:
                self.session = _Session.LOCKED
                self.evidenced = False
            case _Step.UNLOCK:
                self.session = _Session.UNLOCKED
                self.evidenced = True
            case _Step.SUSPEND:
                self.machine = _Machine.ASLEEP
                self.evidenced = False
            case _Step.RESUME:
                self.machine = _Machine.AWAKE
            case _Step.INPUT:
                if self.machine is _Machine.AWAKE and self.session is _Session.UNLOCKED:
                    self.evidenced = True

    def interval_type(self) -> IntervalType:
        if not self.observing and not self.reconstructing:
            return IntervalType.DOWNTIME
        if self.machine is _Machine.OFF:
            return IntervalType.OFF
        if self.machine is _Machine.ASLEEP:
            return IntervalType.ASLEEP
        if self.session is _Session.LOCKED:
            return IntervalType.AWAY
        if self.evidenced:
            return IntervalType.PRESENT
        return IntervalType.UNEVIDENCED


def _by_time(event: Event) -> tuple[datetime, str]:
    return (event.ts, event.id)


def _deduplicated(events: Iterable[Event]) -> list[Event]:
    """Drop repeated `eventlog` records: the same event type at the same full-precision time with the same `detail`, which carries the channel and the code. The first by id is kept."""
    seen: set[tuple[EventType, datetime, str | None]] = set()
    kept: list[Event] = []
    for event in events:
        if event.source is Source.EVENTLOG:
            key = (event.event, event.ts, event.data.get("detail"))
            if key in seen:
                continue
            seen.add(key)
        kept.append(event)
    return kept


def _boundary(event: Event, step: _Step, confidence: Confidence | None = None) -> _Boundary:
    return _Boundary(event.ts, event.id, step, event.source, confidence or event.confidence)


def _paired(events: Sequence[Event], kinds: set[EventType]) -> list[_Boundary]:
    """One boundary per run of same-kind transitions of `kinds` with no opposite one and no `collector_stop` between them (roadmap section 6, timestamp authority). Within a run the live and inferred transitions pair in order with the `eventlog` ones: a matched pair keeps only the `eventlog` time, an unmatched transition keeps its own. Every later boundary in a run repeats a state already reached, so the run yields its earliest."""
    boundaries: list[_Boundary] = []
    run: list[Event] = []
    for event in [*events, None]:
        ends_run = event is None or event.event is EventType.COLLECTOR_STOP
        if run and (ends_run or (event.event in kinds and event.event is not run[0].event)):
            authority = [member for member in run if member.source is Source.EVENTLOG]
            others = [member for member in run if member.source is not Source.EVENTLOG]
            first = min([*authority, *others[len(authority) :]], key=_by_time)
            boundaries.append(_boundary(first, _STEP_OF[first.event]))
            run = []
        if event is not None and event.event in kinds:
            run.append(event)
    return boundaries


type _Plan = dict[str, tuple[_Step, ...]]


def _reconstructed(stop: Event, records: Sequence[Event]) -> _Plan:
    """The steps of a gap both logs cover: the stop carries the state through, and each boot record inside applies. A boot start with no shutdown record since the gap's start or the last shutdown means the machine went down at a time no record shows: observation is lost from the last record before that boot, or from the stop when there is none, up to the boot. A boot start straight after another, with no record between them, is the same start (Kernel-General 12 and EventLog 6005)."""
    plan: _Plan = {stop.id: (_Step.CARRY,)}
    last: Event | None = None
    shut_down = False
    for record in records:
        step = _BOOT_STEPS.get(record.data.get("boot_kind", "")) if record.event is EventType.BOOT else None
        if record.event is EventType.BOOT and step is None:
            continue
        if step is _Step.BOOT_STOP:
            shut_down = True
        elif step is _Step.BOOT_START:
            same_start = last is not None and plan.get(last.id, ())[:1] == (_Step.BOOT_START,)
            if not shut_down and not same_start:
                if last is None:
                    plan[stop.id] = (_Step.STOP,)
                else:
                    plan[last.id] = (*plan.get(last.id, ()), _Step.STOP)
            shut_down = False
        if step is not None:
            plan[record.id] = (step,)
        last = record
    return plan


def _gap_steps(
    stop: Event, start: Event, events: Sequence[Event], coverage: Sequence[Coverage], micro_max: timedelta
) -> _Plan:
    """How one gap between runs is read (roadmap section 8), as the steps its stop and records take, by event id. Empty for a gap that stays `downtime`: a deliberate stop, or an uncovered gap no rule applies to.

    Covered (a coverage note says both logs were readable and reach back over the whole gap): reconstructed (`_reconstructed`). Otherwise, in order: the collector's `boot_time()` line inside the gap marks it `off` up to the boot, and `away` from the boot on, as after any boot start; a gap shorter than `micro_max` is folded, carrying the state through; anything else is `downtime`.
    """
    if stop.data.get("reason") in DELIBERATE_REASONS:
        return {}
    records = [event for event in events if stop.ts < event.ts < start.ts and event.event in _RECORD_EVENTS]
    if any(covered.covers(stop.ts, start.ts) for covered in coverage):
        return _reconstructed(stop, records)
    booted = [
        event
        for event in records
        if event.event is EventType.BOOT
        and event.source is Source.COLLECTOR
        and event.data.get("boot_kind") == BootKind.START
    ]
    if booted:
        return {stop.id: (_Step.SHUTDOWN,), booted[-1].id: (_Step.BOOT_START,)}
    if start.ts - stop.ts < micro_max:
        return {stop.id: (_Step.CARRY,)}
    return {}


def _planned(events: Sequence[Event], micro_max: timedelta) -> _Plan:
    """The steps each gap's events take, by event id: in place of a stop's or a boot's own, beside a session or machine record's. An empty tuple drops the event's boundary. A gap runs from the first `collector_stop` after a run to the next `collector_start`. A phase 2 `suspended` stop is a sleep, not a gap. A further stop inside a gap that is not `downtime` is dropped, so it cannot end the reconstruction."""
    coverage = [covered for event in events if (covered := read_coverage(event)) is not None]
    planned: _Plan = {}
    opener: Event | None = None
    further: list[Event] = []
    for event in events:
        if event.event is EventType.COLLECTOR_STOP and event.data.get("reason") != _LEGACY_SUSPENDED:
            if opener is None:
                opener = event
            else:
                further.append(event)
        elif event.event is EventType.COLLECTOR_START:
            if opener is not None:
                steps = _gap_steps(opener, event, events, coverage, micro_max)
                planned.update(steps)
                if steps:
                    planned.update(dict.fromkeys((stop.id for stop in further), ()))
            opener = None
            further = []
    return planned


def _boundaries(events: Sequence[Event], micro_max: timedelta) -> list[_Boundary]:
    """Every state change in one host's events, in the order of the times replay places them at."""
    planned = _planned(events, micro_max)
    boundaries = _paired(events, _SESSION_EVENTS) + _paired(events, _MACHINE_EVENTS)
    for event in events:
        if event.id in planned:
            # Same time and id as the record's own boundary, which `_paired` put first: a stable sort keeps it first.
            # A stop planned on a record is the last record before a boot no shutdown record precedes: the record is observed as itself, not as the moment the machine went down, so the stop is `inferred`.
            boundaries.extend(
                _boundary(event, step, Confidence.INFERRED if _lost_at_record(event, step) else None)
                for step in planned[event.id]
            )
            continue
        if event.event not in _COLLECTOR_EVENTS:
            continue
        reason = event.data.get("reason")
        if event.event is EventType.COLLECTOR_STOP and reason == _LEGACY_SUSPENDED:
            boundaries.append(_boundary(event, _Step.SUSPEND, Confidence.INFERRED))
        elif event.event is EventType.COLLECTOR_START and reason == _LEGACY_RESUMED:
            boundaries.append(_boundary(event, _Step.START, Confidence.INFERRED))
        else:
            boundaries.append(_boundary(event, _STEP_OF[event.event]))
    return sorted(boundaries, key=lambda boundary: (boundary.ts, boundary.id))


def _lost_at_record(event: Event, step: _Step) -> bool:
    return step is _Step.STOP and event.event is not EventType.COLLECTOR_STOP


def _inferred_edge(boundary: _Boundary | None) -> bool:
    return boundary is not None and boundary.confidence is Confidence.INFERRED


def _inferred_close(boundary: _Boundary | None) -> bool:
    return _inferred_edge(boundary) and boundary is not None and boundary.step not in _STOP_STEPS


def _intervals(host: str, events: Sequence[Event], micro_max: timedelta) -> list[Interval]:
    """The typed timeline of one host, from its first `collector_start` on. Events before it only set the state that start inherits. A span opened inside a reconstructed or folded gap is `inferred` whatever its boundaries say, so the gap's edges split an interval of the same type. A stop that closes an interval does not make it `inferred`: presence observed up to an unclean stop stays observed."""
    state = _State()
    spans: list[tuple[IntervalType, bool, _Boundary, _Boundary | None]] = []
    for boundary in _boundaries(_deduplicated(events), micro_max):
        state.apply(boundary.step)
        if not spans and boundary.step is not _Step.START:
            continue
        kind = state.interval_type()
        if spans and spans[-1][0] is kind and spans[-1][1] == state.reconstructing:
            continue
        if spans:
            previous_kind, previous_reconstructed, opened, _ = spans[-1]
            spans[-1] = (previous_kind, previous_reconstructed, opened, boundary)
        spans.append((kind, state.reconstructing, boundary, None))

    intervals: list[Interval] = []
    for kind, reconstructed, opened, closed in spans:
        end = None if closed is None else closed.ts
        if end == opened.ts:
            continue
        inferred = reconstructed or _inferred_edge(opened) or _inferred_close(closed)
        confidence = Confidence.INFERRED if inferred else Confidence.OBSERVED
        previous = intervals[-1] if intervals else None
        if previous is not None and previous.kind is kind and previous.confidence is confidence:
            intervals[-1] = Interval(kind, previous.start, end, host, confidence, previous.source)
        else:
            intervals.append(Interval(kind, opened.ts, end, host, confidence, opened.source))
    return intervals


def day_view(intervals: Iterable[Interval], day: date, tz: tzinfo, now: datetime) -> tuple[Interval, ...]:
    """The intervals clipped to `day`, from its local midnight in `tz` to the next, as seen at `now` (aware UTC). Storage is never split; only this view is. An open interval ends at `now` or the day's end, whichever is first; an interval starting at or after `now` is dropped."""
    day_start = datetime.combine(day, time(), tz).astimezone(UTC)
    day_end = datetime.combine(day + timedelta(days=1), time(), tz).astimezone(UTC)
    clipped: list[Interval] = []
    for interval in intervals:
        if interval.start >= now:
            continue
        start = max(interval.start, day_start)
        end = min(now if interval.end is None else interval.end, day_end)
        if end <= start:
            continue
        clipped.append(
            Interval(interval.kind, start, end, interval.host, interval.confidence, interval.source)
        )
    return tuple(clipped)


@dataclass(slots=True)
class _HostState:
    open_start: Event | None = None
    last_stop: Event | None = None


def _inferred(event: Event) -> bool:
    return event.confidence is Confidence.INFERRED


def replay(events: Iterable[Event], *, micro_max: timedelta = timedelta()) -> Timeline:
    """Derive the typed timeline (roadmap section 6) and the collector's runs. Input order is not trusted: events are sorted by `(ts, id)` first.

    Typed intervals: present means awake, unlocked and evidenced. `unlock`, and `input` while awake on an unlocked session, open presence; `lock`, `suspend` and a `collector_stop` close it. Collector liveness with no session event is `unevidenced`, never `present`. Live and inferred session and machine transitions take their `eventlog` partner's time, never across a `collector_stop`; repeated `eventlog` records count once. A phase 2 `suspended` stop with its `resumed` start reads as an inferred suspend and resume.

    Gaps between runs (roadmap section 8): a gap after a deliberate stop is `downtime`, and an `unlock` inside it opens presence only from the next start. A gap a coverage note says both logs cover is reconstructed: the state before it carries through, the records inside it apply, a shutdown record makes it `off` and a start record `away` until an unlock, and every interval inside is `inferred`. Its only `downtime` is before a start record no shutdown record precedes, from the last record before that start. An uncovered gap holding the collector's `boot_time()` line is `off` up to it and `away` from it until an unlock, `inferred`. Any other uncovered gap shorter than `micro_max` is folded: the state carries through, `inferred`. Anything else is `downtime`. `micro_max` is `absence.micro_max`; the default folds nothing.

    Collector runs: each host's `collector_start` pairs with its next `collector_stop`, and that stop with the next start. A start followed by another start leaves the first interval unclosed (`end=None`) and produces no gap, since where it ended is unknown. A stop with no open interval produces nothing. Events of any other type are kept and ignored for pairing.
    """
    ordered = tuple(sorted(events, key=_by_time))
    states: dict[str, _HostState] = {}
    by_host: dict[str, list[Event]] = {}
    presences: list[Presence] = []
    gaps: list[Gap] = []

    for event in ordered:
        by_host.setdefault(event.host, []).append(event)
        state = states.setdefault(event.host, _HostState())
        if event.event is EventType.COLLECTOR_START:
            if state.open_start is not None:
                presences.append(Presence(state.open_start.ts, None, event.host, end_inferred=False))
            elif state.last_stop is not None:
                gaps.append(
                    Gap(
                        start=state.last_stop.ts,
                        end=event.ts,
                        host=event.host,
                        start_inferred=_inferred(state.last_stop),
                        end_inferred=_inferred(event),
                        reason=state.last_stop.data.get("reason"),
                    )
                )
            state.open_start = event
            state.last_stop = None
        elif event.event is EventType.COLLECTOR_STOP and state.open_start is not None:
            presences.append(
                Presence(state.open_start.ts, event.ts, event.host, end_inferred=_inferred(event))
            )
            state.open_start = None
            state.last_stop = event

    for host, state in states.items():
        if state.open_start is not None:
            presences.append(Presence(state.open_start.ts, None, host, end_inferred=False))

    intervals = [
        interval
        for host, host_events in by_host.items()
        for interval in _intervals(host, host_events, micro_max)
    ]
    return Timeline(
        events=ordered,
        intervals=tuple(sorted(intervals, key=lambda interval: (interval.start, interval.host))),
        presences=tuple(sorted(presences, key=lambda presence: (presence.start, presence.host))),
        gaps=tuple(sorted(gaps, key=lambda gap: (gap.start, gap.host))),
    )
