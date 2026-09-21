from datetime import date, time, timedelta

import pytest

from timeexisting.domain.resolver import resolve
from timeexisting.domain.schedule import DayFlag, build_day
from timeexisting.domain.segments import Phase

_MONDAY = date(2026, 9, 14)
_SATURDAY = date(2026, 9, 19)
_STARTS = (time(8, 0), time(8, 30), time(9, 0))


def _minutes(plan):
    moment = plan.segments[0].start
    end = plan.segments[-1].end
    while moment < end:
        yield moment
        moment += timedelta(minutes=1)


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
