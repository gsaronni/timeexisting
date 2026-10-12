"""Replay the ledger into a timeline (roadmap sections 5 and 6), per host: the typed intervals of the presence model, plus the collector's own runs and the gaps between them.

Pure. No clock, no I/O, no Rich: the caller reads the ledger and hands the events in. Nothing is classified here; a gap carries the reason its opening stop recorded and nothing more.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import Enum, StrEnum, auto

from timeexisting.ledger.events import Confidence, Event, EventType, NoteKind, Source

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
    """One interval of the typed timeline on `host`. `end` is `None` while it is still open. `confidence` is `inferred` when either boundary was inferred; `source` is the opening boundary's."""

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
    UNKNOWN = auto()


class _Session(Enum):
    UNLOCKED = auto()
    LOCKED = auto()
    UNKNOWN = auto()


class _Step(Enum):
    START = auto()
    STOP = auto()
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
# Phase 2 wrote a clock jump as a stop and start pair with these reasons (`StopReason.SUSPENDED`, `StartReason.RESUMED` in the collector); replay reads them as an inferred suspend and resume.
_LEGACY_SUSPENDED = "suspended"
_LEGACY_RESUMED = "resumed"


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
    """The three facts of the presence model (machine, session, observation) plus whether presence is evidenced."""

    observing: bool = False
    machine: _Machine = _Machine.UNKNOWN
    session: _Session = _Session.UNKNOWN
    evidenced: bool = False

    def apply(self, step: _Step) -> None:
        match step:
            case _Step.START:
                self.observing = True
                self.machine = _Machine.AWAKE
            case _Step.STOP:
                # Loss of observation closes presence and leaves machine and session unknown; reconstruction from the event logs (section 8) is not applied here.
                self.observing = False
                self.machine = _Machine.UNKNOWN
                self.session = _Session.UNKNOWN
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
        if not self.observing:
            return IntervalType.DOWNTIME
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


def _boundaries(events: Sequence[Event]) -> list[_Boundary]:
    """Every state change in one host's events, in the order of the times replay places them at."""
    boundaries = _paired(events, _SESSION_EVENTS) + _paired(events, _MACHINE_EVENTS)
    for event in events:
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


def _inferred_edge(boundary: _Boundary | None) -> bool:
    return boundary is not None and boundary.confidence is Confidence.INFERRED


def _intervals(host: str, events: Sequence[Event]) -> list[Interval]:
    """The typed timeline of one host, from its first `collector_start` on. Events before it only set the state that start inherits."""
    state = _State()
    spans: list[tuple[IntervalType, _Boundary, _Boundary | None]] = []
    for boundary in _boundaries(_deduplicated(events)):
        state.apply(boundary.step)
        if not spans and boundary.step is not _Step.START:
            continue
        kind = state.interval_type()
        if spans and spans[-1][0] is kind:
            continue
        if spans:
            previous_kind, opened, _ = spans[-1]
            spans[-1] = (previous_kind, opened, boundary)
        spans.append((kind, boundary, None))

    intervals: list[Interval] = []
    for kind, opened, closed in spans:
        end = None if closed is None else closed.ts
        if end == opened.ts:
            continue
        inferred = _inferred_edge(opened) or _inferred_edge(closed)
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


def replay(events: Iterable[Event]) -> Timeline:
    """Derive the typed timeline (roadmap section 6) and the collector's runs. Input order is not trusted: events are sorted by `(ts, id)` first.

    Typed intervals: present means awake, unlocked and evidenced. `unlock`, and `input` while awake on an unlocked session, open presence; `lock`, `suspend` and every `collector_stop` close it. A stop is a loss of observation and gives `downtime` up to the next start, except a phase 2 `suspended` stop, which with its `resumed` start reads as an inferred suspend and resume. Collector liveness with no session event is `unevidenced`, never `present`. `off` is not produced here yet: phase 3 step 6 turns `boot` records and `boot_time()` inside a gap into `off`. Live and inferred session and machine transitions take their `eventlog` partner's time, never across a `collector_stop`; repeated `eventlog` records count once.

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
        interval for host, host_events in by_host.items() for interval in _intervals(host, host_events)
    ]
    return Timeline(
        events=ordered,
        intervals=tuple(sorted(intervals, key=lambda interval: (interval.start, interval.host))),
        presences=tuple(sorted(presences, key=lambda presence: (presence.start, presence.host))),
        gaps=tuple(sorted(gaps, key=lambda gap: (gap.start, gap.host))),
    )
