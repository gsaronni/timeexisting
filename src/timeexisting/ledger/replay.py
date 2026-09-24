"""Replay the ledger into a timeline (roadmap section 5): the collector's presence intervals and the gaps between them, per host.

Pure. No clock, no I/O, no Rich: the caller reads the ledger and hands the events in. Nothing is classified here; a gap carries the reason its opening stop recorded and nothing more.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from timeexisting.ledger.events import Confidence, Event, EventType


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
