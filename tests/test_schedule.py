from datetime import date, datetime, time, timedelta

import pytest

from timeexisting.config.loader import load_config
from timeexisting.domain.schedule import DayFlag, build_day
from timeexisting.domain.segments import Phase, Segment

_MONDAY = date(2026, 9, 14)
_SATURDAY = date(2026, 9, 19)
_SUNDAY = date(2026, 9, 20)


@pytest.fixture
def cfg():
    return load_config()


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


def test_working_day_segments_are_contiguous_non_overlapping_and_span_the_day(cfg):
    plan = build_day(_MONDAY, time(8, 30), frozenset(), cfg)

    for previous, current in zip(plan.segments, plan.segments[1:], strict=False):
        assert previous.end == current.start

    # Domain segments are intentionally naive: a calendar day's wall-clock
    # shape, not an aware instant. See domain/schedule.py's module docstring.
    assert plan.segments[0].start == datetime(2026, 9, 14, 0, 0)  # noqa: DTZ001
    assert plan.segments[-1].end == datetime(2026, 9, 15, 0, 0)  # noqa: DTZ001


def test_night_window_produces_off_hours_at_both_ends(cfg):
    plan = build_day(_MONDAY, time(8, 30), frozenset(), cfg)

    assert plan.segments[0].phase is Phase.OFF_HOURS
    assert plan.segments[0].end.time() == cfg.credit.night_end
    assert plan.segments[-1].phase is Phase.OFF_HOURS
    assert plan.segments[-1].start.time() == cfg.credit.night_start


@pytest.mark.parametrize("day", [_SATURDAY, _SUNDAY])
def test_weekend_is_a_single_off_day_segment(cfg, day):
    plan = build_day(day, time(9, 0), frozenset(), cfg)

    assert plan.segments == (
        Segment(
            start=datetime.combine(day, time()),
            end=datetime.combine(day, time()) + timedelta(days=1),
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
