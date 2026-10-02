import re
import tempfile
from datetime import UTC, date, datetime, time, timedelta
from importlib.resources import files
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from timeexisting import paths
from timeexisting.config import loader
from timeexisting.content.art import ART_DIR, MANIFEST
from timeexisting.domain.resolver import resolve
from timeexisting.domain.schedule import DayFlag, build_day
from timeexisting.ui.app import _update
from timeexisting.ui.layout import build_layout
from timeexisting.ui.panels import day, header, week, year
from timeexisting.ui.theme import load_theme

THEME = load_theme()

_MONDAY = date(2026, 9, 14)
_SATURDAY = date(2026, 9, 19)
_SUNDAY = date(2026, 9, 20)

# One case per `Phase`, plus a weekend day and both dedicated off-day flags,
# so every day.py branch is exercised as its own, independently reportable
# test case rather than one iteration of a loop.
_CASE_IDS = [
    "off_hours",
    "pre_work",
    "working_morning",
    "lunch",
    "working_afternoon",
    "post_work",
    "saturday",
    "sunday",
    "sick",
    "afspadsering",
    "offsite",
]

# One `now` per Phase, plus a couple of month/season edges for year.py.
_YEAR_MOMENTS = [
    datetime(2026, 9, 14, 7, 30, tzinfo=UTC),
    datetime(2026, 9, 19, 14, 0, tzinfo=UTC),  # Saturday
    datetime(2026, 9, 20, 16, 0, tzinfo=UTC),  # Sunday
    datetime(2026, 1, 1, 10, 0, tzinfo=UTC),  # January, Winter
    datetime(2026, 12, 31, 10, 0, tzinfo=UTC),  # December, Winter
    datetime(2026, 2, 28, 10, 0, tzinfo=UTC),  # short month edge
]


def _render_to_string(renderable, width: int = 100) -> str:
    console = Console(file=StringIO(), width=width, record=True, legacy_windows=False)
    console.print(renderable)
    return console.export_text()


@pytest.fixture(scope="session")
def cfg():
    """Session-scoped override of the shared (function-scoped) `cfg` fixture
    from conftest.py: this file parametrizes ~20 day/week panel cases, and a
    fresh config load per case buys nothing since none of them touch a local
    config.toml. `monkeypatch` itself is function-scoped only, so isolation
    from a real local config.toml is done by hand with `pytest.MonkeyPatch`
    and undone immediately after `load_config()` returns.
    """
    mp = pytest.MonkeyPatch()
    missing = Path(tempfile.gettempdir()) / "te-test-no-such-config.toml"
    mp.setattr(paths, "config_file", lambda: missing)
    try:
        return loader.load_config()
    finally:
        mp.undo()


@pytest.fixture(scope="session")
def moments(cfg):
    """One `Resolved` per case id in `_CASE_IDS`, computed once for the
    session and looked up by id from each parametrized test.
    """
    start = time(8, 30)
    plan = build_day(_MONDAY, start, frozenset(), cfg)
    saturday_plan = build_day(_SATURDAY, start, frozenset(), cfg)
    sunday_plan = build_day(_SUNDAY, start, frozenset(), cfg)
    sick_plan = build_day(_MONDAY, start, frozenset({DayFlag.SICK}), cfg)
    afspadsering_plan = build_day(_MONDAY, start, frozenset({DayFlag.AFSPADSERING}), cfg)
    offsite_plan = build_day(_MONDAY, start, frozenset({DayFlag.OFFSITE}), cfg)

    off_hours, pre_work, working_morning, lunch, working_afternoon, post_work, _ = plan.segments

    return {
        "off_hours": resolve(off_hours.start + timedelta(hours=2), plan),
        "pre_work": resolve(pre_work.start + timedelta(minutes=30), plan),
        "working_morning": resolve(working_morning.start + timedelta(minutes=30), plan),
        "lunch": resolve(lunch.start + timedelta(minutes=10), plan),
        "working_afternoon": resolve(working_afternoon.start + timedelta(hours=1), plan),
        "post_work": resolve(post_work.start + timedelta(minutes=30), plan),
        "saturday": resolve(saturday_plan.segments[0].start + timedelta(hours=10), saturday_plan),
        "sunday": resolve(sunday_plan.segments[0].start + timedelta(hours=16), sunday_plan),
        "sick": resolve(sick_plan.segments[0].start + timedelta(hours=10), sick_plan),
        "afspadsering": resolve(afspadsering_plan.segments[0].start + timedelta(hours=10), afspadsering_plan),
        "offsite": resolve(offsite_plan.segments[0].start + timedelta(hours=10), offsite_plan),
    }


def test_header_renders():
    assert _render_to_string(header.render(THEME))


@pytest.mark.parametrize("now", _YEAR_MOMENTS)
def test_year_panel_renders_for_every_moment(now):
    assert _render_to_string(year.render(now, THEME))


@pytest.mark.parametrize("case", _CASE_IDS)
def test_week_panel_renders_for_every_moment(case, moments, cfg):
    assert _render_to_string(week.render(moments[case], cfg, THEME))


@pytest.mark.parametrize("case", _CASE_IDS)
def test_day_panel_renders_for_every_moment(case, moments):
    assert _render_to_string(day.render(moments[case], THEME))


def test_pre_work_never_shows_negative_countdown(cfg):
    plan = build_day(_MONDAY, time(8, 30), frozenset(), cfg)
    resolved = resolve(plan.start - timedelta(minutes=1), plan)
    rendered = _render_to_string(day.render(resolved, THEME))
    assert "-" not in rendered.split("Work begins in:")[1].split("\n")[0]


@pytest.mark.parametrize("width", [100, 140, 200])
def test_header_art_keeps_the_leading_whitespace_of_the_file(width):
    """Every art line sits at the file's own indent plus one shared offset.

    Regression: the art was justified line by line, so lines of different
    lengths were centred separately and the last one visibly shifted.
    """
    raw = files("timeexisting").joinpath(ART_DIR).joinpath(MANIFEST["header"]).read_text(encoding="utf-8")
    expected = [line.rstrip() for line in raw.rstrip().split("\n")]
    inner_width = width - 4  # border plus panel padding, both sides

    rows = _render_to_string(header.render(THEME), width=width).split("\n")
    rendered = [row[1:-1].rstrip() for row in rows[1 : 1 + len(expected)]]

    offsets = set()
    for got, want in zip(rendered, expected, strict=True):
        offset = len(got) - len(got.lstrip()) - (len(want) - len(want.lstrip()))
        offsets.add(offset)
        assert got.lstrip() == want.lstrip()[: inner_width - (len(want) - len(want.lstrip()))].rstrip()
    assert len(offsets) == 1


_BAR_WIDTHS = [80, 140, 200]
_THIN_BAR = "━"
_LARGE_BAR = "█"


def _bar_lines(renderable, width: int) -> list[str]:
    """Rendered rows that carry a thin bar, after checking nothing overflows."""
    rows = _render_to_string(renderable, width=width).rstrip("\n").split("\n")
    assert all(len(row) <= width for row in rows)
    return [row for row in rows if _THIN_BAR in row]


@pytest.mark.parametrize("width", _BAR_WIDTHS)
def test_year_panel_shows_a_thin_bar_beside_each_percentage(width):
    year_row, month_row = _bar_lines(year.render(_YEAR_MOMENTS[0], THEME), width)

    assert "Year Progress" in year_row
    assert "70.4%" in year_row
    assert "September - 46.7%" in month_row
    assert year_row.index(_THIN_BAR) == month_row.index(_THIN_BAR)
    assert year_row.count(_THIN_BAR) > month_row.count(_THIN_BAR) > 0


@pytest.mark.parametrize("width", _BAR_WIDTHS)
def test_week_panel_shows_a_thin_bar_beside_its_percentage(width, moments, cfg):
    (row,) = _bar_lines(week.render(moments["working_afternoon"], cfg, THEME), width)
    assert "% of the workweek complete" in row
    assert row.index("%") < row.index(_THIN_BAR)


@pytest.mark.parametrize("width", _BAR_WIDTHS)
def test_week_panel_has_no_bar_when_it_shows_no_percentage(width, moments, cfg):
    assert _bar_lines(week.render(moments["saturday"], cfg, THEME), width) == []


@pytest.mark.parametrize("width", _BAR_WIDTHS)
def test_day_panel_keeps_the_only_large_bar(width, moments, cfg):
    assert _LARGE_BAR not in _render_to_string(year.render(_YEAR_MOMENTS[0], THEME), width=width)
    assert _LARGE_BAR not in _render_to_string(
        week.render(moments["working_afternoon"], cfg, THEME), width=width
    )

    rendered = _render_to_string(day.render(moments["working_afternoon"], THEME), width=width)
    assert _LARGE_BAR in rendered
    assert _THIN_BAR not in rendered


# Filled and unfilled part of a thin bar; rows of box borders are skipped.
_THIN_BAR_RUN = re.compile("[━─]+")
_BORDER_CORNERS = set("╭╮╰╯")


def _render_as_seen(renderable, width: int, *, no_color: bool, height: int = 60) -> list[str]:
    """Rendered rows as a terminal shows them, glyph for glyph, with colour on or off."""
    console = Console(
        file=StringIO(),
        width=width,
        height=height,
        color_system="truecolor",
        force_terminal=True,
        legacy_windows=False,
        no_color=no_color,
        record=True,
    )
    console.print(renderable)
    return console.export_text().rstrip("\n").split("\n")


def _thin_bar_spans(rows: list[str]) -> list[tuple[int, int]]:
    """(start column, length) of every thin bar, top to bottom."""
    return [
        match.span()
        for row in rows
        if not _BORDER_CORNERS & set(row)
        for match in _THIN_BAR_RUN.finditer(row)
    ]


@pytest.mark.parametrize("no_color", [False, True])
@pytest.mark.parametrize("now", _YEAR_MOMENTS)
@pytest.mark.parametrize("width", [40, 80, 140, 200])
def test_year_and_month_bars_share_start_column_and_length(width, now, no_color):
    spans = _thin_bar_spans(_render_as_seen(year.render(now, THEME), width, no_color=no_color))
    assert len(spans) == 2
    (year_start, year_end), (month_start, month_end) = spans
    assert year_start == month_start
    assert year_end - year_start == month_end - month_start >= 8


@pytest.mark.parametrize("no_color", [False, True])
@pytest.mark.parametrize("width", [80, 130, 140, 200])
def test_year_and_month_bars_align_in_the_dashboard(width, no_color, moments, cfg):
    """The same check through the real layout, where the panel gets half the width.

    Measured on the glyphs alone, as a terminal with colour off or a track
    colour close to its background shows them: the visible bars, not just the
    cells they occupy, must start together and be equally long.
    """
    layout = build_layout(THEME)
    _update(layout, moments["working_afternoon"], cfg, THEME)
    rows = [row[: width // 2] for row in _render_as_seen(layout, width, no_color=no_color)]

    year_span, month_span, week_span = _thin_bar_spans(rows)
    assert year_span == month_span
    assert week_span[1] - week_span[0] >= 8
