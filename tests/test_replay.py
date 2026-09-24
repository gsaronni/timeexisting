import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from timeexisting.ledger.events import Confidence, Event, EventType, Source
from timeexisting.ledger.replay import Gap, Presence, Timeline, replay

_T0 = datetime(2026, 9, 17, 6, 0, 0, tzinfo=UTC)
_CPH = ZoneInfo("Europe/Copenhagen")


def _at(minutes: int) -> datetime:
    return _T0 + timedelta(minutes=minutes)


def _start(ts: datetime, host: str = "laptop", reason: str = "launch", **overrides) -> Event:
    return Event.new(
        ts=ts,
        host=host,
        profile="work",
        event=EventType.COLLECTOR_START,
        source=Source.COLLECTOR,
        data={"reason": reason},
        **overrides,
    )


def _stop(ts: datetime, host: str = "laptop", reason: str = "signal", **overrides) -> Event:
    return Event.new(
        ts=ts,
        host=host,
        profile="work",
        event=EventType.COLLECTOR_STOP,
        source=Source.COLLECTOR,
        data={"reason": reason},
        **overrides,
    )


def test_empty_ledger_gives_an_empty_timeline():
    assert replay([]) == Timeline(events=(), presences=(), gaps=())


def test_start_and_stop_give_one_interval():
    timeline = replay([_start(_at(0)), _stop(_at(60))])
    assert timeline.presences == (Presence(_at(0), _at(60), "laptop", end_inferred=False),)
    assert timeline.gaps == ()


def test_stop_then_start_give_a_gap_carrying_the_stops_reason():
    timeline = replay([_start(_at(0)), _stop(_at(60), reason="stop_file"), _start(_at(90))])
    assert timeline.gaps == (
        Gap(_at(60), _at(90), "laptop", start_inferred=False, end_inferred=False, reason="stop_file"),
    )
    assert timeline.presences == (
        Presence(_at(0), _at(60), "laptop", end_inferred=False),
        Presence(_at(90), None, "laptop", end_inferred=False),
    )


def test_inferred_stop_marks_the_interval_end_and_the_gap_start():
    for reason in ("unclean", "suspended"):
        timeline = replay(
            [
                _start(_at(0)),
                _stop(_at(60), reason=reason, confidence=Confidence.INFERRED),
                _start(_at(75), reason="resumed"),
                _stop(_at(120)),
            ]
        )
        assert timeline.presences[0] == Presence(_at(0), _at(60), "laptop", end_inferred=True)
        assert timeline.gaps == (
            Gap(_at(60), _at(75), "laptop", start_inferred=True, end_inferred=False, reason=reason),
        )


def test_inferred_start_marks_the_gap_end():
    timeline = replay([_start(_at(0)), _stop(_at(60)), _start(_at(75), confidence=Confidence.INFERRED)])
    assert timeline.gaps[0].end_inferred is True


def test_stop_without_a_reason_gives_a_gap_with_no_reason():
    stop = Event.new(
        ts=_at(60), host="laptop", profile="work", event=EventType.COLLECTOR_STOP, source=Source.COLLECTOR
    )
    timeline = replay([_start(_at(0)), stop, _start(_at(90))])
    assert timeline.gaps[0].reason is None


def test_start_start_gives_an_unclosed_interval_and_no_gap():
    timeline = replay([_start(_at(0)), _start(_at(60)), _stop(_at(90))])
    assert timeline.presences == (
        Presence(_at(0), None, "laptop", end_inferred=False),
        Presence(_at(60), _at(90), "laptop", end_inferred=False),
    )
    assert timeline.gaps == ()


def test_a_hosts_final_start_is_open():
    timeline = replay([_start(_at(0)), _stop(_at(30)), _start(_at(45))])
    assert timeline.presences[-1] == Presence(_at(45), None, "laptop", end_inferred=False)


def test_stop_with_no_open_interval_produces_nothing_and_is_kept():
    orphan = _stop(_at(0))
    timeline = replay([orphan, _start(_at(30))])
    assert timeline.presences == (Presence(_at(30), None, "laptop", end_inferred=False),)
    assert timeline.gaps == ()
    assert orphan in timeline.events


def test_a_second_stop_does_not_move_the_gap_start():
    timeline = replay([_start(_at(0)), _stop(_at(30), reason="signal"), _stop(_at(40)), _start(_at(60))])
    assert timeline.gaps == (
        Gap(_at(30), _at(60), "laptop", start_inferred=False, end_inferred=False, reason="signal"),
    )


def test_other_event_types_are_kept_and_ignored_for_pairing():
    note = Event.new(
        ts=_at(10),
        host="laptop",
        profile="work",
        event=EventType.NOTE,
        source=Source.MANUAL,
        data={"text": "hi"},
    )
    lock = Event.new(ts=_at(20), host="laptop", profile="work", event=EventType.LOCK, source=Source.WIN32)
    timeline = replay([_start(_at(0)), note, lock, _stop(_at(30))])
    assert timeline.events[1:3] == (note, lock)
    assert timeline.presences == (Presence(_at(0), _at(30), "laptop", end_inferred=False),)


def test_unsorted_input_gives_the_same_timeline():
    events = [_start(_at(0)), _stop(_at(30)), _start(_at(45)), _stop(_at(90)), _start(_at(120))]
    shuffled = events[:]
    random.Random(7).shuffle(shuffled)
    assert replay(shuffled) == replay(events)
    assert replay(shuffled).events == tuple(events)


def test_equal_timestamps_are_ordered_by_id():
    stop = _stop(_at(30))
    start = _start(_at(30))
    timeline = replay([start, _start(_at(0)), stop])
    assert timeline.events[1:] == (stop, start)
    assert timeline.gaps == (
        Gap(_at(30), _at(30), "laptop", start_inferred=False, end_inferred=False, reason="signal"),
    )


def test_two_hosts_are_kept_separate():
    timeline = replay(
        [
            _start(_at(0), host="laptop"),
            _start(_at(5), host="desktop"),
            _stop(_at(30), host="laptop"),
            _start(_at(40), host="laptop"),
            _stop(_at(50), host="desktop"),
        ]
    )
    assert timeline.presences == (
        Presence(_at(0), _at(30), "laptop", end_inferred=False),
        Presence(_at(5), _at(50), "desktop", end_inferred=False),
        Presence(_at(40), None, "laptop", end_inferred=False),
    )
    assert timeline.gaps == (
        Gap(_at(30), _at(40), "laptop", start_inferred=False, end_inferred=False, reason="signal"),
    )


def test_a_run_crossing_local_midnight_is_one_continuous_interval():
    start = datetime(2026, 9, 17, 23, 30, tzinfo=_CPH)
    stop = datetime(2026, 9, 18, 0, 45, tzinfo=_CPH)
    timeline = replay([_start(start), _stop(stop)])
    assert timeline.presences == (Presence(start.astimezone(UTC), stop.astimezone(UTC), "laptop", False),)


def test_a_run_across_the_autumn_fall_back_stays_ordered_in_utc():
    # 25 October 2026: local 03:00 CEST becomes 02:00 CET, so 02:xx happens twice.
    local = [
        (datetime(2026, 10, 25, 1, 50, tzinfo=_CPH), _start),
        (datetime(2026, 10, 25, 2, 40, fold=0, tzinfo=_CPH), _stop),
        (datetime(2026, 10, 25, 2, 10, fold=1, tzinfo=_CPH), _start),
        (datetime(2026, 10, 25, 2, 50, fold=1, tzinfo=_CPH), _stop),
    ]
    events = [make(ts) for ts, make in local]
    timeline = replay(reversed(events))

    stamps = [event.ts for event in timeline.events]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)
    assert all(ts.tzinfo is UTC for ts in stamps)
    assert [presence.end - presence.start for presence in timeline.presences] == [
        timedelta(minutes=50),
        timedelta(minutes=40),
    ]
    (gap,) = timeline.gaps
    assert gap.end - gap.start == timedelta(minutes=30)
