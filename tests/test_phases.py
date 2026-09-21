from datetime import UTC, date, datetime, timedelta

import pytest

from timeexisting.domain.phases import Phase, resolve_phase, resolve_work_progress

WEEKDAY = date(2026, 9, 14)  # Monday
SATURDAY = date(2026, 9, 19)
SUNDAY = date(2026, 9, 20)

MINUTES_IN_A_DAY = 24 * 60


def _minutes_of(day: date) -> list[datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return [start + timedelta(minutes=m) for m in range(MINUTES_IN_A_DAY)]


@pytest.mark.parametrize("day", [WEEKDAY, SATURDAY, SUNDAY])
def test_exactly_one_phase_per_minute(day):
    for now in _minutes_of(day):
        phase = resolve_phase(now)
        assert isinstance(phase, Phase)


def test_weekend_days_are_always_weekend():
    for day in (SATURDAY, SUNDAY):
        for now in _minutes_of(day):
            assert resolve_phase(now) is Phase.WEEKEND


def test_weekday_never_reports_weekend():
    for now in _minutes_of(WEEKDAY):
        assert resolve_phase(now) is not Phase.WEEKEND


def test_weekday_work_progress_has_no_negative_duration_or_percentage():
    for now in _minutes_of(WEEKDAY):
        if resolve_phase(now) is not Phase.WORK:
            continue
        progress = resolve_work_progress(now)
        assert progress.elapsed >= timedelta(0)
        assert progress.remaining >= timedelta(0)
        assert 0 <= progress.percentage <= 100
        assert 0 <= progress.message_bucket <= 9


def test_pre_work_is_never_misclassified_as_work():
    pre_work = datetime(2026, 9, 14, 7, 30, tzinfo=UTC)
    assert resolve_phase(pre_work) is Phase.PRE_WORK


def test_break_windows_bracket_correctly():
    just_before = datetime(2026, 9, 14, 10, 59, tzinfo=UTC)
    start = datetime(2026, 9, 14, 11, 0, tzinfo=UTC)
    end = datetime(2026, 9, 14, 11, 15, tzinfo=UTC)
    assert resolve_phase(just_before) is Phase.WORK
    assert resolve_phase(start) is Phase.MORNING_BREAK
    assert resolve_phase(end) is Phase.WORK
