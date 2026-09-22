from datetime import UTC, date, datetime, time, timedelta
from io import StringIO

import pytest
from rich.console import Console

from timeexisting.domain.resolver import resolve
from timeexisting.domain.schedule import DayFlag, build_day
from timeexisting.ui.panels import day, header, week, year
from timeexisting.ui.theme import load_theme

THEME = load_theme()

_MONDAY = date(2026, 9, 14)
_SATURDAY = date(2026, 9, 19)
_SUNDAY = date(2026, 9, 20)

# One `now` per Phase, plus a couple of month/season edges for year.py.
_YEAR_MOMENTS = [
    datetime(2026, 9, 14, 7, 30, tzinfo=UTC),
    datetime(2026, 9, 19, 14, 0, tzinfo=UTC),  # Saturday
    datetime(2026, 9, 20, 16, 0, tzinfo=UTC),  # Sunday
    datetime(2026, 1, 1, 10, 0, tzinfo=UTC),  # January, Winter
    datetime(2026, 12, 31, 10, 0, tzinfo=UTC),  # December, Winter
    datetime(2026, 2, 28, 10, 0, tzinfo=UTC),  # short month edge
]


def _render_to_string(renderable) -> str:
    console = Console(file=StringIO(), width=100, record=True)
    console.print(renderable)
    return console.export_text()


@pytest.fixture
def moments(cfg):
    """One `Resolved` per `Phase`, plus a weekend day and both dedicated
    off-day flags, so every day.py branch renders at least once.
    """
    start = time(8, 30)
    plan = build_day(_MONDAY, start, frozenset(), cfg)
    saturday_plan = build_day(_SATURDAY, start, frozenset(), cfg)
    sunday_plan = build_day(_SUNDAY, start, frozenset(), cfg)
    sick_plan = build_day(_MONDAY, start, frozenset({DayFlag.SICK}), cfg)
    afspadsering_plan = build_day(_MONDAY, start, frozenset({DayFlag.AFSPADSERING}), cfg)
    offsite_plan = build_day(_MONDAY, start, frozenset({DayFlag.OFFSITE}), cfg)

    off_hours, pre_work, working_morning, lunch, working_afternoon, post_work, _ = plan.segments

    return [
        resolve(off_hours.start + timedelta(hours=2), plan),  # off_hours
        resolve(pre_work.start + timedelta(minutes=30), plan),  # pre_work
        resolve(working_morning.start + timedelta(minutes=30), plan),  # working (morning)
        resolve(lunch.start + timedelta(minutes=10), plan),  # lunch
        resolve(working_afternoon.start + timedelta(hours=1), plan),  # working (afternoon)
        resolve(post_work.start + timedelta(minutes=30), plan),  # post_work
        resolve(saturday_plan.segments[0].start + timedelta(hours=10), saturday_plan),  # weekend (Saturday)
        resolve(sunday_plan.segments[0].start + timedelta(hours=16), sunday_plan),  # weekend (Sunday)
        resolve(sick_plan.segments[0].start + timedelta(hours=10), sick_plan),  # off_day: sick
        resolve(afspadsering_plan.segments[0].start + timedelta(hours=10), afspadsering_plan),
        resolve(offsite_plan.segments[0].start + timedelta(hours=10), offsite_plan),  # off_day: fallback
    ]


def test_header_renders():
    assert _render_to_string(header.render(THEME))


@pytest.mark.parametrize("now", _YEAR_MOMENTS)
def test_year_panel_renders_for_every_moment(now):
    assert _render_to_string(year.render(now, THEME))


def test_week_panel_renders_for_every_moment(moments, cfg):
    for resolved in moments:
        assert _render_to_string(week.render(resolved, cfg, THEME))


def test_day_panel_renders_for_every_moment(moments):
    for resolved in moments:
        assert _render_to_string(day.render(resolved, THEME))


def test_pre_work_never_shows_negative_countdown(cfg):
    plan = build_day(_MONDAY, time(8, 30), frozenset(), cfg)
    resolved = resolve(plan.start - timedelta(minutes=1), plan)
    rendered = _render_to_string(day.render(resolved, THEME))
    assert "-" not in rendered.split("Work begins in:")[1].split("\n")[0]
