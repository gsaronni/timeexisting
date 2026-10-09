"""Replay the ledger into a timeline (roadmap section 5): the collector's presence intervals and the gaps between them, per host.

Pure. No clock, no I/O, no Rich: the caller reads the ledger and hands the events in. Nothing is classified here; a gap carries the reason its opening stop recorded and nothing more.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from timeexisting.ledger.events import Confidence, Event, EventType, NoteKind

_BOOLEANS = {"true": True, "false": False}


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
    """Every event in `(ts, id)` order, nothing collapsed, plus the presence intervals and gaps derived from them, each sorted by start then host."""

    events: tuple[Event, ...]
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


@dataclass(slots=True)
class _HostState:
    open_start: Event | None = None
    last_stop: Event | None = None


def _inferred(event: Event) -> bool:
    return event.confidence is Confidence.INFERRED


def replay(events: Iterable[Event]) -> Timeline:
    """Pair each host's `collector_start` with its next `collector_stop`, and that stop with the next start. Input order is not trusted: events are sorted by `(ts, id)` first.

    A start followed by another start leaves the first interval unclosed (`end=None`) and produces no gap, since where it ended is unknown. A stop with no open interval produces nothing. Events of any other type are kept and ignored for pairing.
    """
    ordered = tuple(sorted(events, key=lambda event: (event.ts, event.id)))
    states: dict[str, _HostState] = {}
    presences: list[Presence] = []
    gaps: list[Gap] = []

    for event in ordered:
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

    return Timeline(
        events=ordered,
        presences=tuple(sorted(presences, key=lambda presence: (presence.start, presence.host))),
        gaps=tuple(sorted(gaps, key=lambda gap: (gap.start, gap.host))),
    )
