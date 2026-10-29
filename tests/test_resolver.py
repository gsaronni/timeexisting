from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from timeexisting.domain.resolver import resolve
from timeexisting.domain.schedule import DayFlag, build_day
from timeexisting.domain.segments import Phase

_MONDAY = date(2026, 9, 14)
_SATURDAY = date(2026, 9, 19)
# The three flex-band starts of the roadmap table, a start inside the lunch
# window, and a start after it (the prod screen of 28 October 2026).
_STARTS = (time(8, 0), time(8, 30), time(9, 0), time(11, 10), time(15, 21))
# 28 October 2026, a Wednesday: the prod and dev screens of the same afternoon.
_SCREEN_DAY = date(2026, 10, 28)


def _minutes(plan):
    moment = plan.segments[0].start
    end = plan.segments[-1].end
    while moment < end:
        yield moment
        moment += timedelta(minutes=1)


def _window_minutes(plan):
    moment = plan.start
    while moment <= plan.nominal_end:
        yield moment
        moment += timedelta(minutes=1)


def _remaining_deduction(now, plan) -> timedelta:
    """The unpaid time of `plan` still ahead between `now` and the nominal end."""
    return sum(
        (
            max(timedelta(0), min(segment.end, plan.nominal_end) - max(segment.start, now))
            for segment in plan.segments
            if not segment.paid
        ),
        timedelta(0),
    )


def _local(cfg, day: date, moment: time) -> datetime:
    return datetime.combine(day, moment, tzinfo=ZoneInfo(cfg.display.timezone))


@pytest.mark.parametrize("start", _STARTS)
def test_every_minute_resolves_to_exactly_one_segment_with_no_negative_values(cfg, start):
    plan = build_day(_MONDAY, start, frozenset(), cfg)

    for moment in _minutes(plan):
        resolved = resolve(moment, plan)
        assert resolved.elapsed >= timedelta(0)
        assert resolved.remaining >= timedelta(0)
        assert resolved.expected_credit >= timedelta(0)
        assert resolved.elapsed + resolved.remaining == resolved.segment.end - resolved.segment.start
        assert resolved.segment.start <= moment < resolved.segment.end


@pytest.mark.parametrize("start", _STARTS)
def test_expected_credit_is_monotonic_and_reaches_target_exactly_at_nominal_end(cfg, start):
    plan = build_day(_MONDAY, start, frozenset(), cfg)

    previous = timedelta(0)
    for moment in _minutes(plan):
        resolved = resolve(moment, plan)
        assert resolved.expected_credit >= previous
        assert resolved.expected_credit <= plan.target
        previous = resolved.expected_credit

    at_nominal_end = resolve(plan.nominal_end, plan)
    assert at_nominal_end.expected_credit == plan.target
    assert at_nominal_end.past_nominal_end is True

    just_before = resolve(plan.nominal_end - timedelta(minutes=1), plan)
    assert just_before.past_nominal_end is False


@pytest.mark.parametrize("start", _STARTS)
def test_progress_reaches_exactly_one_at_nominal_end_and_never_exceeds_it(cfg, start):
    plan = build_day(_MONDAY, start, frozenset(), cfg)

    for moment in _minutes(plan):
        resolved = resolve(moment, plan)
        assert 0.0 <= resolved.progress <= 1.0

    assert resolve(plan.nominal_end, plan).progress == 1.0
    assert resolve(plan.nominal_end - timedelta(minutes=1), plan).progress < 1.0


@pytest.mark.parametrize("start", _STARTS)
def test_credit_before_the_start_is_zero_at_every_minute(cfg, start):
    plan = build_day(_MONDAY, start, frozenset(), cfg)

    for moment in _minutes(plan):
        if moment > plan.start:
            break
        assert resolve(moment, plan).expected_credit == timedelta(0), moment


@pytest.mark.parametrize("start", _STARTS)
def test_target_minus_credit_equals_remaining_minus_the_remaining_deduction_at_every_minute(cfg, start):
    """The bar and the window line can never disagree: what the bar still has to credit is what Remaining counts down, less the unpaid time still ahead of `now`. Checked at every minute of the window, start to nominal end."""
    plan = build_day(_MONDAY, start, frozenset(), cfg)

    for moment in _window_minutes(plan):
        resolved = resolve(moment, plan)
        assert plan.target - resolved.expected_credit == resolved.remaining_to_end - _remaining_deduction(
            moment, plan
        ), moment


def test_progress_at_a_start_after_the_lunch_window_is_zero_and_agrees_with_remaining(cfg):
    """The prod screen: start 15:21, now 15:21:11. The window line said 15:21 to 23:15 and Remaining 07:53:48, both right; the bar said 52%, from an afternoon block that opened at 11:30. Now the lunch window sits at the start, nothing is credited for its 30m, and the bar reads 0% in agreement with Remaining."""
    plan = build_day(_SCREEN_DAY, time(15, 21), frozenset(), cfg)
    now = _local(cfg, _SCREEN_DAY, time(15, 21, 11))
    resolved = resolve(now, plan)

    assert plan.start == _local(cfg, _SCREEN_DAY, time(15, 21))
    assert plan.nominal_end == _local(cfg, _SCREEN_DAY, time(23, 15))
    assert resolved.remaining_to_end == timedelta(hours=7, minutes=53, seconds=49)
    assert resolved.segment.phase is Phase.LUNCH
    assert resolved.next_segment is not None
    assert resolved.next_segment.phase is Phase.WORKING

    assert resolved.expected_credit == timedelta(0)
    assert int(resolved.progress * 100) == 0
    assert _remaining_deduction(now, plan) == timedelta(minutes=29, seconds=49)
    assert plan.target - resolved.expected_credit == resolved.remaining_to_end - _remaining_deduction(
        now, plan
    )

    first_working_minute = resolve(_local(cfg, _SCREEN_DAY, time(15, 52)), plan)
    assert first_working_minute.segment.phase is Phase.WORKING
    assert first_working_minute.expected_credit == timedelta(minutes=1)


def test_control_progress_at_a_morning_start_agrees_with_the_window(cfg):
    """The dev screen the same afternoon: start 08:08, now 15:22:50, bar 91%. Unchanged by the fix and pins the arithmetic the late start follows: lunch already taken, so the remaining deduction is zero."""
    plan = build_day(_SCREEN_DAY, time(8, 8), frozenset(), cfg)
    now = _local(cfg, _SCREEN_DAY, time(15, 22, 50))
    resolved = resolve(now, plan)

    assert resolved.expected_credit == timedelta(hours=6, minutes=44, seconds=50)
    assert int(resolved.progress * 100) == 91
    assert _remaining_deduction(now, plan) == timedelta(0)
    assert plan.target - resolved.expected_credit == resolved.remaining_to_end


def test_segment_matches_the_expected_phase_at_key_moments(cfg):
    plan = build_day(_MONDAY, time(8, 30), frozenset(), cfg)

    before_start = resolve(plan.start - timedelta(minutes=1), plan)
    assert before_start.segment.phase is Phase.PRE_WORK

    during_lunch = resolve(plan.start.replace(hour=11, minute=15), plan)
    assert during_lunch.segment.phase is Phase.LUNCH

    at_start = resolve(plan.start, plan)
    assert at_start.segment.phase is Phase.WORKING

    after_nominal_end = resolve(plan.nominal_end + timedelta(minutes=1), plan)
    assert after_nominal_end.segment.phase is Phase.POST_WORK

    at_midnight = resolve(plan.segments[0].start, plan)
    assert at_midnight.segment.phase is Phase.OFF_HOURS


def test_next_segment_is_none_only_for_the_last_segment_of_the_day(cfg):
    plan = build_day(_MONDAY, time(8, 30), frozenset(), cfg)

    resolved = resolve(plan.segments[0].start, plan)
    assert resolved.next_segment is plan.segments[1]

    last = resolve(plan.segments[-1].start, plan)
    assert last.next_segment is None


def test_off_day_plan_resolves_without_crashing_and_has_zero_progress(cfg):
    plan = build_day(_SATURDAY, time(9, 0), frozenset(), cfg)
    resolved = resolve(plan.segments[0].start, plan)

    assert resolved.segment.phase is Phase.OFF_DAY
    assert resolved.expected_credit == timedelta(0)
    assert resolved.progress == 0.0
    assert resolved.next_segment is None


def test_sick_day_resolves_without_crashing(cfg):
    plan = build_day(_MONDAY, time(9, 0), frozenset({DayFlag.SICK}), cfg)
    resolved = resolve(plan.segments[0].start, plan)

    assert resolved.segment.phase is Phase.OFF_DAY
    assert resolved.segment.label == "sick"
