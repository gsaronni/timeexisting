from datetime import UTC, datetime
from io import StringIO

import pytest
from rich.console import Console

from timeexisting.ui.panels import day, header, week, year
from timeexisting.ui.theme import load_theme

THEME = load_theme()

# One `now` per Phase, plus a couple of month/season edges for year.py.
MOMENTS = [
    datetime(2026, 9, 14, 7, 30, tzinfo=UTC),  # Monday, pre-work
    datetime(2026, 9, 14, 11, 5, tzinfo=UTC),  # Monday, morning break
    datetime(2026, 9, 14, 13, 30, tzinfo=UTC),  # Monday, lunch
    datetime(2026, 9, 14, 16, 5, tzinfo=UTC),  # Monday, afternoon break
    datetime(2026, 9, 14, 10, 0, tzinfo=UTC),  # Monday, work
    datetime(2026, 9, 14, 18, 30, tzinfo=UTC),  # Monday, post-work
    datetime(2026, 9, 19, 14, 0, tzinfo=UTC),  # Saturday, weekend
    datetime(2026, 9, 20, 16, 0, tzinfo=UTC),  # Sunday, weekend
    datetime(2026, 1, 1, 10, 0, tzinfo=UTC),  # January, Winter
    datetime(2026, 12, 31, 10, 0, tzinfo=UTC),  # December, Winter
    datetime(2026, 2, 28, 10, 0, tzinfo=UTC),  # short month edge
]


def _render_to_string(renderable) -> str:
    console = Console(file=StringIO(), width=100, record=True)
    console.print(renderable)
    return console.export_text()


def test_header_renders():
    assert _render_to_string(header.render(THEME))


@pytest.mark.parametrize("now", MOMENTS)
def test_year_panel_renders_for_every_moment(now):
    assert _render_to_string(year.render(now, THEME))


@pytest.mark.parametrize("now", MOMENTS)
def test_week_panel_renders_for_every_moment(now):
    assert _render_to_string(week.render(now, THEME))


@pytest.mark.parametrize("now", MOMENTS)
def test_day_panel_renders_for_every_moment(now):
    assert _render_to_string(day.render(now, THEME))


def test_pre_work_never_shows_negative_countdown():
    now = datetime(2026, 9, 14, 7, 30, tzinfo=UTC)
    rendered = _render_to_string(day.render(now, THEME))
    assert "-" not in rendered.split("Work begins in:")[1].split("\n")[0]
