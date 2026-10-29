from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from timeexisting.domain.resolver import resolve
from timeexisting.domain.schedule import DayFlag, build_day
from timeexisting.domain.segments import Phase, Segment

_MONDAY = date(2026, 9, 14)
_SATURDAY = date(2026, 9, 19)
_SUNDAY = date(2026, 9, 20)
# DST ends 25 October 2026 (always the last Sunday of October in the EU, so
# it's never a working day); the Monday right after is the first weekday
# in the new UTC+1 offset.
_DAY_AFTER_DST_TRANSITION = date(2026, 10, 26)
# The prod screen of 28 October 2026 (a Wednesday): a start resolved by rule
# 2 at 15:21, after the lunch window, whose bar read 52% eleven seconds in.
_LATE_START_DAY = date(2026, 10, 28)
_LATE_START = time(15, 21)


@pytest.fixture
def zone(cfg):
    return ZoneInfo(cfg.display.timezone)


def _every_minute_of_the_day():
    moment = datetime.combine(_MONDAY, time())
    while moment.date() == _MONDAY:
        yield moment.time()
        moment += timedelta(minutes=1)


def _unpaid_between(plan, begin, end) -> timedelta:
    """The unpaid time of `plan` inside `[begin, end)`."""
    return sum(
        (
            max(timedelta(0), min(segment.end, end) - max(segment.start, begin))
            for segment in plan.segments
            if not segment.paid
        ),
        timedelta(0),
    )


@pytest.mark.parametrize(
    ("start", "morning", "afternoon", "nominal_end"),
    [
        (time(8, 0), timedelta(hours=3), timedelta(hours=4, minutes=24), time(15, 54)),
        (time(8, 30), timedelta(hours=2, minutes=30), timedelta(hours=4, minutes=54), time(16, 24)),
        (time(9, 0), timedelta(hours=2), timedelta(hours=5, minutes=24), time(16, 54)),
    ],
)
def test_working_day_matches_the_roadmap_table(cfg, start, morning, afternoon, nominal_end):
    plan = build_day(_MONDAY, start, frozenset(), cfg)

    assert plan.nominal_end.time() == nominal_end
    working = [segment for segment in plan.segments if segment.phase is Phase.WORKING]
    assert len(working) == 2
    assert working[0].end - working[0].start == morning
    assert working[1].end - working[1].start == afternoon
    assert working[0].start == plan.start


def test_working_day_has_pre_work_lunch_and_post_work_in_order(cfg):
    plan = build_day(_MONDAY, time(8, 30), frozenset(), cfg)
    phases = [segment.phase for segment in plan.segments]
    assert phases == [
        Phase.OFF_HOURS,
        Phase.PRE_WORK,
        Phase.WORKING,
        Phase.LUNCH,
        Phase.WORKING,
        Phase.POST_WORK,
        Phase.OFF_HOURS,
    ]


def test_working_day_segments_are_contiguous_non_overlapping_and_span_the_day(cfg, zone):
    plan = build_day(_MONDAY, time(8, 30), frozenset(), cfg)

    for previous, current in zip(plan.segments, plan.segments[1:], strict=False):
        assert previous.end == current.start

    assert plan.segments[0].start == datetime(2026, 9, 14, 0, 0, tzinfo=zone)
    assert plan.segments[-1].end == datetime(2026, 9, 15, 0, 0, tzinfo=zone)


def test_segments_are_aware_in_the_configured_timezone(cfg, zone):
    plan = build_day(_MONDAY, time(8, 30), frozenset(), cfg)
    for segment in plan.segments:
        assert segment.start.tzinfo == zone
        assert segment.end.tzinfo == zone


def test_nominal_end_stays_wall_clock_correct_after_the_dst_transition(cfg):
    plan = build_day(_DAY_AFTER_DST_TRANSITION, time(8, 30), frozenset(), cfg)
    assert plan.nominal_end.time() == time(16, 24)
    assert plan.nominal_end.utcoffset() == timedelta(hours=1)  # CET, not CEST


def test_night_window_produces_off_hours_at_both_ends(cfg):
    plan = build_day(_MONDAY, time(8, 30), frozenset(), cfg)

    assert plan.segments[0].phase is Phase.OFF_HOURS
    assert plan.segments[0].end.time() == cfg.credit.night_end
    assert plan.segments[-1].phase is Phase.OFF_HOURS
    assert plan.segments[-1].start.time() == cfg.credit.night_start


def test_a_start_after_the_lunch_window_opens_no_paid_segment_before_the_start(cfg):
    """The cause of the 52%: before the fix the afternoon block opened at 11:30, 3h51m before a 15:21 start, and was credited at once. Every paid segment opens at or after the start."""
    plan = build_day(_LATE_START_DAY, _LATE_START, frozenset(), cfg)

    paid = [segment for segment in plan.segments if segment.paid]
    assert all(segment.start >= plan.start for segment in paid), [
        (segment.start.time(), segment.end.time()) for segment in paid
    ]


def test_a_start_after_the_lunch_window_moves_the_window_to_the_start_and_keeps_the_deduction(cfg, zone):
    """Start 15:21: the window and the nominal end stay as prod showed them, 15:21 to 23:15. The lunch, 30m unpaid, is the first segment of the day, so the working block from its end reaches the target exactly at 23:15. The nominal end is past `night_start`, so there is no post-work and the night bookend opens at 23:15."""
    plan = build_day(_LATE_START_DAY, _LATE_START, frozenset(), cfg)

    def at(moment: time) -> datetime:
        return datetime.combine(_LATE_START_DAY, moment, tzinfo=zone)

    assert plan.start == at(time(15, 21))
    assert plan.nominal_end == at(time(23, 15))
    assert plan.deduction == cfg.lunch.deduction
    assert [(segment.phase, segment.start, segment.end) for segment in plan.segments] == [
        (Phase.OFF_HOURS, at(time(0, 0)), at(time(6, 0))),
        (Phase.PRE_WORK, at(time(6, 0)), at(time(15, 21))),
        (Phase.LUNCH, at(time(15, 21)), at(time(15, 51))),
        (Phase.WORKING, at(time(15, 51)), at(time(23, 15))),
        (Phase.OFF_HOURS, at(time(23, 15)), at(time(0, 0)) + timedelta(days=1)),
    ]


def test_a_start_inside_the_lunch_window_moves_the_window_to_the_start(cfg, zone):
    """Start 11:10, inside the 11:00 to 11:30 window: before the fix the morning block ran backwards and the afternoon block opened at 11:30 with ten minutes of lunch unaccounted for. The window moves to 11:10 and keeps its 30m."""
    plan = build_day(_MONDAY, time(11, 10), frozenset(), cfg)

    def at(moment: time) -> datetime:
        return datetime.combine(_MONDAY, moment, tzinfo=zone)

    assert [(segment.phase, segment.start, segment.end) for segment in plan.segments] == [
        (Phase.OFF_HOURS, at(time(0, 0)), at(time(6, 0))),
        (Phase.PRE_WORK, at(time(6, 0)), at(time(11, 10))),
        (Phase.LUNCH, at(time(11, 10)), at(time(11, 40))),
        (Phase.WORKING, at(time(11, 40)), at(time(19, 4))),
        (Phase.POST_WORK, at(time(19, 4)), at(time(22, 0))),
        (Phase.OFF_HOURS, at(time(22, 0)), at(time(0, 0)) + timedelta(days=1)),
    ]


def test_a_half_day_ending_before_the_window_closes_moves_the_window_to_the_start(cfg, zone):
    """The same clamp from the other side: a half day from 07:00 ends at 11:12, before the window closes, so the window moves to the start and the one working block, 07:30 to 11:12, is exactly the 3h42m target. A start a minute later than the last one that fits keeps the window in place: from 07:19 the day ends at 11:31 and a one-minute working block follows the lunch."""
    moved = build_day(_MONDAY, time(7, 0), frozenset({DayFlag.HALF_DAY}), cfg)

    def at(moment: time) -> datetime:
        return datetime.combine(_MONDAY, moment, tzinfo=zone)

    assert moved.nominal_end == at(time(11, 12))
    assert [(segment.phase, segment.start, segment.end) for segment in moved.segments[2:5]] == [
        (Phase.LUNCH, at(time(7, 0)), at(time(7, 30))),
        (Phase.WORKING, at(time(7, 30)), at(time(11, 12))),
        (Phase.POST_WORK, at(time(11, 12)), at(time(22, 0))),
    ]

    kept = build_day(_MONDAY, time(7, 19), frozenset({DayFlag.HALF_DAY}), cfg)
    assert [(segment.phase, segment.start, segment.end) for segment in kept.segments[2:5]] == [
        (Phase.WORKING, at(time(7, 19)), at(time(11, 0))),
        (Phase.LUNCH, at(time(11, 0)), at(time(11, 30))),
        (Phase.WORKING, at(time(11, 30)), at(time(11, 31))),
    ]


def test_every_minute_of_the_day_as_a_start_builds_a_consistent_day(cfg):
    """The contract for every start, 00:00 to 23:59: the segments are contiguous from midnight, no paid segment opens before the start, the credit is zero at the start instant, the unpaid time inside the window is exactly the deduction, and the bar reads 100% at the nominal end and not a minute before. A nominal end past midnight is still reached: the day's last segment ends there."""
    for start in _every_minute_of_the_day():
        plan = build_day(_MONDAY, start, frozenset(), cfg)

        assert plan.segments[0].start.time() == time()
        for previous, current in zip(plan.segments, plan.segments[1:], strict=False):
            assert previous.end == current.start, start
        assert plan.segments[-1].end >= plan.nominal_end, start
        assert all(segment.start < segment.end for segment in plan.segments), start

        assert all(segment.start >= plan.start for segment in plan.segments if segment.paid), start
        assert _unpaid_between(plan, plan.start, plan.nominal_end) == plan.deduction, start

        at_start = resolve(plan.start, plan)
        assert at_start.expected_credit == timedelta(0), start
        assert at_start.progress == 0.0, start

        at_nominal_end = resolve(plan.nominal_end, plan)
        assert at_nominal_end.expected_credit == plan.target, start
        assert at_nominal_end.progress == 1.0, start
        just_before = resolve(plan.nominal_end - timedelta(minutes=1), plan)
        assert just_before.progress < 1.0, start


@pytest.mark.parametrize("day", [_SATURDAY, _SUNDAY])
def test_weekend_is_a_single_off_day_segment(cfg, zone, day):
    plan = build_day(day, time(9, 0), frozenset(), cfg)

    assert plan.segments == (
        Segment(
            start=datetime.combine(day, time(), tzinfo=zone),
            end=datetime.combine(day, time(), tzinfo=zone) + timedelta(days=1),
            phase=Phase.OFF_DAY,
            label="weekend",
            paid=False,
        ),
    )
    assert plan.target == timedelta(0)
    assert plan.deduction == timedelta(0)


def test_no_lunch_drops_the_lunch_segment_and_moves_nominal_end_earlier(cfg):
    plain = build_day(_MONDAY, time(8, 30), frozenset(), cfg)
    no_lunch = build_day(_MONDAY, time(8, 30), frozenset({DayFlag.NO_LUNCH}), cfg)

    assert Phase.LUNCH not in [segment.phase for segment in no_lunch.segments]
    assert no_lunch.deduction == timedelta(0)
    assert no_lunch.nominal_end == plain.nominal_end - timedelta(minutes=30)

    working = [segment for segment in no_lunch.segments if segment.phase is Phase.WORKING]
    assert len(working) == 1
    assert working[0].start == no_lunch.start
    assert working[0].end == no_lunch.nominal_end


def test_half_day_halves_the_target_and_keeps_lunch(cfg):
    plan = build_day(_MONDAY, time(8, 30), frozenset({DayFlag.HALF_DAY}), cfg)

    assert plan.target == cfg.contract.daily_target / 2
    assert plan.target == timedelta(hours=3, minutes=42)
    assert Phase.LUNCH in [segment.phase for segment in plan.segments]
    assert plan.nominal_end == plan.start + plan.target + cfg.lunch.deduction


def test_no_lunch_and_half_day_combine(cfg):
    plan = build_day(_MONDAY, time(8, 30), frozenset({DayFlag.NO_LUNCH, DayFlag.HALF_DAY}), cfg)

    assert plan.target == timedelta(hours=3, minutes=42)
    assert plan.deduction == timedelta(0)
    assert Phase.LUNCH not in [segment.phase for segment in plan.segments]
    assert plan.nominal_end == plan.start + timedelta(hours=3, minutes=42)


@pytest.mark.parametrize("flag", [DayFlag.SICK, DayFlag.OFFSITE, DayFlag.AFSPADSERING, DayFlag.FRI])
def test_off_day_flags_produce_a_single_off_day_segment_labelled_by_the_flag(cfg, flag):
    plan = build_day(_MONDAY, time(9, 0), frozenset({flag}), cfg)

    assert len(plan.segments) == 1
    assert plan.segments[0].phase is Phase.OFF_DAY
    assert plan.segments[0].label == flag.value
    assert flag in plan.flags
    assert plan.target == timedelta(0)


def test_off_day_flag_takes_precedence_on_a_weekday_over_the_working_shape(cfg):
    plan = build_day(_MONDAY, time(9, 0), frozenset({DayFlag.SICK}), cfg)
    assert len(plan.segments) == 1
    assert plan.segments[0].phase is Phase.OFF_DAY
