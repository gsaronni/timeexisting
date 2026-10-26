"""Day views of the typed timeline: local-day bounds from a `ZoneInfo`, clipped rows, and the current derived state."""

from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from timeexisting.domain.timeline_view import current_state, day_bounds, day_events, day_rows, hosts_on
from timeexisting.ledger.events import Confidence, Event, EventType, Source
from timeexisting.ledger.replay import Interval, IntervalType, Timeline

ZONE = ZoneInfo("Europe/Copenhagen")


def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _interval(
    kind: IntervalType,
    start: datetime,
    end: datetime | None,
    host: str = "laptop",
    confidence: Confidence = Confidence.OBSERVED,
    source: Source = Source.EVENTLOG,
) -> Interval:
    return Interval(kind, start, end, host, confidence, source)


def _timeline(*intervals: Interval, events: tuple[Event, ...] = ()) -> Timeline:
    return Timeline(events=events, intervals=intervals, presences=(), gaps=())


def _event(ts: datetime, host: str = "laptop") -> Event:
    return Event.new(
        ts=ts,
        host=host,
        profile="work",
        event=EventType.UNLOCK,
        source=Source.EVENTLOG,
        data={"detail": "811"},
    )


# --- day bounds ------------------------------------------------------------


def test_the_autumn_change_day_is_25_hours_long():
    start, end = day_bounds(date(2026, 10, 25), ZONE)
    assert start == _utc(2026, 10, 24, 22)
    assert end == _utc(2026, 10, 25, 23)
    assert end - start == timedelta(hours=25)


def test_the_spring_change_day_is_23_hours_long():
    start, end = day_bounds(date(2026, 3, 29), ZONE)
    assert start == _utc(2026, 3, 28, 23)
    assert end == _utc(2026, 3, 29, 22)
    assert end - start == timedelta(hours=23)


def test_an_ordinary_day_is_24_hours_long():
    start, end = day_bounds(date(2026, 10, 26), ZONE)
    assert start == _utc(2026, 10, 25, 23)
    assert end - start == timedelta(hours=24)


def test_a_fixed_offset_would_get_the_change_day_wrong():
    """The control for the zone rule: the offset `now` carries on 24 October gives 24 hours, which is why the bounds never come from one."""
    cest = timezone(timedelta(hours=2))
    start, end = day_bounds(date(2026, 10, 25), cest)
    assert end - start == timedelta(hours=24)


# --- rows ------------------------------------------------------------------


def test_the_repeated_hour_counts_twice_on_25_october():
    """01:30 CEST to 03:30 CET spans the repeated 02:00 hour: three hours, stored and reported in UTC."""
    start, end = _utc(2026, 10, 24, 23, 30), _utc(2026, 10, 25, 2, 30)
    [row] = day_rows(
        _timeline(_interval(IntervalType.PRESENT, start, end)), "laptop", date(2026, 10, 25), ZONE, end
    )
    assert row.duration == timedelta(hours=3)
    assert (row.start, row.end) == (start, end)
    # The same wall-clock span on an ordinary day is two hours.
    start, end = _utc(2026, 10, 26, 0, 30), _utc(2026, 10, 26, 2, 30)
    [row] = day_rows(
        _timeline(_interval(IntervalType.PRESENT, start, end)), "laptop", date(2026, 10, 26), ZONE, end
    )
    assert row.duration == timedelta(hours=2)


def test_an_interval_crossing_midnight_is_clipped_at_the_days_start():
    crossing = _interval(IntervalType.ASLEEP, _utc(2026, 9, 28, 20), _utc(2026, 9, 29, 6))
    inside = _interval(IntervalType.PRESENT, _utc(2026, 9, 29, 6), _utc(2026, 9, 29, 8))
    rows = day_rows(_timeline(crossing, inside), "laptop", date(2026, 9, 29), ZONE, _utc(2026, 9, 29, 9))
    assert [(row.start, row.end) for row in rows] == [
        (_utc(2026, 9, 28, 22), _utc(2026, 9, 29, 6)),  # 00:00 local on the 29th
        (_utc(2026, 9, 29, 6), _utc(2026, 9, 29, 8)),
    ]
    assert rows[0].duration == timedelta(hours=8)


def test_an_open_interval_today_ends_at_now_and_is_marked_open():
    now = _utc(2026, 9, 29, 9)
    [open_row] = day_rows(
        _timeline(_interval(IntervalType.AWAY, _utc(2026, 9, 29, 8), None)),
        "laptop",
        date(2026, 9, 29),
        ZONE,
        now,
    )
    assert (open_row.end, open_row.open) == (now, True)
    [closed_row] = day_rows(
        _timeline(_interval(IntervalType.AWAY, _utc(2026, 9, 29, 8), now)),
        "laptop",
        date(2026, 9, 29),
        ZONE,
        now,
    )
    assert (closed_row.end, closed_row.open) == (now, False)


def test_an_open_interval_on_a_past_day_ends_at_the_days_end():
    timeline = _timeline(_interval(IntervalType.PRESENT, _utc(2026, 9, 29, 8), None))
    [row] = day_rows(timeline, "laptop", date(2026, 9, 29), ZONE, _utc(2026, 9, 30, 10))
    assert row.end == day_bounds(date(2026, 9, 29), ZONE)[1]
    assert row.open


def test_other_hosts_and_intervals_outside_the_day_are_dropped():
    timeline = _timeline(
        _interval(IntervalType.PRESENT, _utc(2026, 9, 29, 8), _utc(2026, 9, 29, 9), host="desktop"),
        _interval(IntervalType.PRESENT, _utc(2026, 9, 27, 8), _utc(2026, 9, 27, 9)),
        _interval(IntervalType.PRESENT, _utc(2026, 9, 29, 8), _utc(2026, 9, 29, 9)),
    )
    rows = day_rows(timeline, "laptop", date(2026, 9, 29), ZONE, _utc(2026, 9, 29, 10))
    assert [(row.start, row.end) for row in rows] == [(_utc(2026, 9, 29, 8), _utc(2026, 9, 29, 9))]
    assert day_rows(_timeline(), "laptop", date(2026, 9, 29), ZONE, _utc(2026, 9, 29, 10)) == ()


def test_day_events_and_hosts_on_follow_the_same_day_window():
    on_day, before, other = (
        _event(_utc(2026, 9, 29, 6)),
        _event(_utc(2026, 9, 28, 21)),
        _event(_utc(2026, 9, 29, 6), "desktop"),
    )
    touching = _interval(IntervalType.ASLEEP, _utc(2026, 9, 28, 20), _utc(2026, 9, 29, 6), host="tablet")
    yesterday = _interval(IntervalType.ASLEEP, _utc(2026, 9, 28, 10), _utc(2026, 9, 28, 20), host="phone")
    timeline = _timeline(touching, yesterday, events=(before, on_day, other))
    assert day_events(timeline, "laptop", date(2026, 9, 29), ZONE) == (on_day,)
    assert hosts_on(timeline, date(2026, 9, 29), ZONE) == ("desktop", "laptop", "tablet")


# --- current state ---------------------------------------------------------


def test_current_state_is_the_hosts_open_last_interval():
    closed = _interval(IntervalType.PRESENT, _utc(2026, 9, 29, 6), _utc(2026, 9, 29, 8))
    open_away = _interval(IntervalType.AWAY, _utc(2026, 9, 29, 8), None)
    assert current_state(_timeline(closed, open_away), "laptop") == (IntervalType.AWAY, _utc(2026, 9, 29, 8))
    assert current_state(_timeline(closed), "laptop") is None
    assert current_state(_timeline(closed, open_away), "desktop") is None
    assert current_state(_timeline(), "laptop") is None
