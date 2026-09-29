import re
from datetime import date, datetime, time, timedelta
from io import StringIO

from rich.console import Console

from timeexisting.content import phrases
from timeexisting.domain.resolver import resolve
from timeexisting.domain.schedule import DayFlag, build_day
from timeexisting.domain.week import remaining_week
from timeexisting.ui.app import local_timezone
from timeexisting.ui.panels import week
from timeexisting.ui.theme import load_theme

_MONDAY = date(2026, 9, 14)
_START = time(8, 30)
_DAY = timedelta(hours=7, minutes=24)


def _resolved(cfg, day: date, hour: int, minute: int = 0, flags: frozenset[DayFlag] = frozenset()):
    plan = build_day(day, _START, flags, cfg)
    return resolve(datetime.combine(day, time(hour, minute), tzinfo=local_timezone()), plan)


def test_before_monday_starts_the_whole_week_remains(cfg):
    assert remaining_week(_resolved(cfg, _MONDAY, 7), cfg) == cfg.contract.weekly_target


def test_monday_mid_morning_is_todays_window_plus_four_days(cfg):
    # 08:30 to 10:00 is 1h30 worked: 5h54 left today, plus 4 x 7h24.
    assert remaining_week(_resolved(cfg, _MONDAY, 10), cfg) == timedelta(hours=5, minutes=54) + 4 * _DAY


def test_lunch_is_not_counted_as_remaining_work(cfg):
    during_lunch = remaining_week(_resolved(cfg, _MONDAY, 11, 15), cfg)
    after_lunch = remaining_week(_resolved(cfg, _MONDAY, 11, 30), cfg)
    assert during_lunch == after_lunch


def test_friday_after_the_end_leaves_nothing(cfg):
    assert remaining_week(_resolved(cfg, _MONDAY + timedelta(days=4), 18), cfg) == timedelta(0)


def test_the_weekend_leaves_nothing(cfg):
    assert remaining_week(_resolved(cfg, _MONDAY + timedelta(days=5), 12), cfg) == timedelta(0)
    assert remaining_week(_resolved(cfg, _MONDAY + timedelta(days=6), 12), cfg) == timedelta(0)


def test_todays_flags_come_through_the_plan(cfg):
    sick = remaining_week(_resolved(cfg, _MONDAY, 7, flags=frozenset({DayFlag.SICK})), cfg)
    half = remaining_week(_resolved(cfg, _MONDAY, 7, flags=frozenset({DayFlag.HALF_DAY})), cfg)
    assert sick == 4 * _DAY
    assert half == _DAY / 2 + 4 * _DAY


def test_excluded_later_days_do_not_count(cfg):
    asked: list[date] = []

    def holiday_on_wednesday(day: date) -> bool:
        asked.append(day)
        return day.weekday() == 2

    assert remaining_week(_resolved(cfg, _MONDAY, 7), cfg, excluded=holiday_on_wednesday) == 4 * _DAY
    # Only the later working-day candidates are asked about; today comes from its plan.
    assert _MONDAY not in asked
    assert max(asked) <= _MONDAY + timedelta(days=6)


def test_the_week_ends_on_sunday_not_seven_days_later(cfg):
    thursday_early = remaining_week(_resolved(cfg, _MONDAY + timedelta(days=3), 7), cfg)
    assert thursday_early == 2 * _DAY


def test_weekday_lines_carry_no_hardcoded_figures():
    for weekday in range(7):
        for line in phrases._pool(phrases.DEFAULT_VOICE, phrases.DEFAULT_LOCALE, f"weekday.{weekday}"):
            assert not re.search(r"\d", line), line


def test_monday_comment_shows_the_planned_remaining_week(cfg):
    console = Console(file=StringIO(), width=200, record=True)
    console.print(week.render(_resolved(cfg, _MONDAY, 10), cfg, load_theme()))
    text = console.export_text()
    assert "35h30m of corporate servitude remain" in text
    assert "96 hours" not in text
