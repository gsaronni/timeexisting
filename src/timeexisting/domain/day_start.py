"""The day's start from the ledger (roadmap section 4, start-time resolution): a `day_start` the user typed or confirmed, else the first instant of the local day at which the typed timeline (section 6) says `present`. Collector liveness is never a start.

Rules, highest first:

1. The latest `day_start` of this host whose `start` falls on the current local day. Provenance `flag` or `prompt`, from its `detail`.
2. The first instant of the local day, at or after `credit.earliest`, at which this host is `present`. A present interval opened before the clamp and still open at it starts the day at the clamp; one that closes before it is never a start. Provenance `logon` when the interval was opened by the session logon time, else `observed` or `inferred` from its confidence.
3. The session logon time reaches the ledger as an `unlock`, source `logon`, so rule 2 serves it.
4. The machine's boot time is a sanity clamp, never a start: a present interval opened before it belongs to a session the ledger has lost track of, and does not count.
5. The prompt, which is the caller's: this module returns `None` when the ledger holds no start.

Pure: no clock, no I/O. `now` carries the local zone and defines the day.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time
from enum import StrEnum

from timeexisting.config.models import Config
from timeexisting.ledger.events import Confidence, EventType, Source
from timeexisting.ledger.replay import IntervalType, Timeline


class Provenance(StrEnum):
    """Where a resolved start came from. The viewer renders `inferred` differently from the rest."""

    FLAG = "flag"
    PROMPT = "prompt"
    OBSERVED = "observed"
    LOGON = "logon"
    INFERRED = "inferred"


@dataclass(frozen=True, slots=True)
class DayStart:
    """A resolved start: the local wall-clock time, to the minute, and its provenance."""

    start: time
    provenance: Provenance


_DETAIL_OF = {"flag": Provenance.FLAG}


def _recorded(timeline: Timeline, host: str, now: datetime) -> DayStart | None:
    """Rule 1. `timeline.events` are in `order_key` order, so the last match is the latest record and supersedes the rest. A record whose `start` is missing, not ISO 8601 or naive is ignored."""
    found: DayStart | None = None
    for event in timeline.events:
        if event.event is not EventType.DAY_START or event.host != host:
            continue
        try:
            start = datetime.fromisoformat(event.data.get("start", ""))
        except ValueError:
            continue
        if start.tzinfo is None or start.utcoffset() is None:
            continue
        local = start.astimezone(now.tzinfo)
        if local.date() != now.date():
            continue
        found = DayStart(_minute(local), _DETAIL_OF.get(event.data.get("detail", ""), Provenance.PROMPT))
    return found


def _derived(
    timeline: Timeline, host: str, now: datetime, cfg: Config, boot: datetime | None
) -> DayStart | None:
    """Rules 2 and 4. Intervals are in start order, so the first that qualifies is the day's first present instant."""
    earliest = datetime.combine(now.date(), cfg.credit.earliest, tzinfo=now.tzinfo).astimezone(UTC)
    now_utc = now.astimezone(UTC)
    for interval in timeline.intervals:
        if interval.host != host or interval.kind is not IntervalType.PRESENT:
            continue
        if boot is not None and interval.start < boot:
            continue
        opened = max(interval.start, earliest)
        closed = now_utc if interval.end is None else min(interval.end, now_utc)
        if opened >= closed:
            continue
        if interval.source is Source.LOGON:
            provenance = Provenance.LOGON
        elif interval.confidence is Confidence.INFERRED:
            provenance = Provenance.INFERRED
        else:
            provenance = Provenance.OBSERVED
        return DayStart(_minute(opened.astimezone(now.tzinfo)), provenance)
    return None


def _minute(moment: datetime) -> time:
    return moment.time().replace(second=0, microsecond=0)


def resolve_day_start(
    timeline: Timeline, host: str, now: datetime, cfg: Config, *, boot: datetime | None = None
) -> DayStart | None:
    """The start of `now`'s local day for `host`, or `None` when the ledger holds none and the caller must ask. `boot` is the machine's boot time, aware UTC, or `None` when it could not be read, in which case rule 4 applies no clamp."""
    recorded = _recorded(timeline, host, now)
    if recorded is not None:
        return recorded
    return _derived(timeline, host, now, cfg, boot)
