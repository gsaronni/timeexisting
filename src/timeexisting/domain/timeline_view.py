"""Views of the typed timeline (roadmap section 6): the rows of one local day, as `te collect timeline` prints them, and the current derived state, as the viewer's footer and `te collect status` show it.

Pure: the caller supplies the timeline, the day, the zone and `now`. Intervals stay in UTC underneath; a local day is only a window onto them, built from a `ZoneInfo` so that a DST day is 23 or 25 hours long, never from a fixed offset.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from timeexisting.ledger.events import Confidence, Event, Source
from timeexisting.ledger.replay import IntervalType, Timeline


@dataclass(frozen=True, slots=True)
class DayRow:
    """One interval clipped to a local day. `start` and `end` are aware UTC. `open` says the interval had no end in the ledger: it ran to `now` when the day is today, else to the day's end."""

    kind: IntervalType
    start: datetime
    end: datetime
    confidence: Confidence
    source: Source
    open: bool

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class CollectorState:
    """What the footer reports. `running` says a collector holds its lock. `kind` is the open interval's type on this host, or `None` when the ledger holds no open interval; `since` is that interval's start, the collector's start when it runs without one, or the last checkpoint tick when it does not run."""

    running: bool
    kind: IntervalType | None
    since: datetime | None


def day_bounds(day: date, zone: tzinfo) -> tuple[datetime, datetime]:
    """00:00 on `day` and 00:00 on the next day, built in `zone` and converted to UTC. On 25 October 2026 the window is 25 hours long and on 29 March 2026 23."""
    start = datetime.combine(day, time(0), tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone).astimezone(UTC)
    return start, end


def day_rows(timeline: Timeline, host: str, day: date, zone: tzinfo, now: datetime) -> tuple[DayRow, ...]:
    """`host`'s intervals clipped to `day` in `zone`, in start order. An open interval ends at `now`, which the day's end then clips on a past day; a clipped interval that would be empty is dropped."""
    day_start, day_end = day_bounds(day, zone)
    now_utc = now.astimezone(UTC)
    rows: list[DayRow] = []
    for interval in timeline.intervals:
        if interval.host != host:
            continue
        end = now_utc if interval.end is None else interval.end
        start = max(interval.start, day_start)
        end = min(end, day_end)
        if start >= end:
            continue
        rows.append(
            DayRow(interval.kind, start, end, interval.confidence, interval.source, open=interval.end is None)
        )
    return tuple(rows)


def day_events(timeline: Timeline, host: str, day: date, zone: tzinfo) -> tuple[Event, ...]:
    """`host`'s events whose timestamp falls on `day` in `zone`, in replay order."""
    day_start, day_end = day_bounds(day, zone)
    return tuple(event for event in timeline.events if event.host == host and day_start <= event.ts < day_end)


def hosts_on(timeline: Timeline, day: date, zone: tzinfo) -> tuple[str, ...]:
    """Every host with an interval touching `day` or an event on it, sorted."""
    day_start, day_end = day_bounds(day, zone)
    found = {event.host for event in timeline.events if day_start <= event.ts < day_end}
    for interval in timeline.intervals:
        if interval.start < day_end and (interval.end is None or interval.end > day_start):
            found.add(interval.host)
    return tuple(sorted(found))


def current_state(timeline: Timeline, host: str) -> tuple[IntervalType, datetime] | None:
    """`host`'s open interval as `(kind, start)`, or `None` when its last interval is closed or it has none. Intervals are in start order, so the last one for the host is the current one."""
    current = None
    for interval in timeline.intervals:
        if interval.host == host:
            current = interval
    if current is None or current.end is not None:
        return None
    return current.kind, current.start
