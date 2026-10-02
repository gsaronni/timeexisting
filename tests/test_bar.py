from io import StringIO

import pytest
from rich.console import Console
from rich.style import Style

from timeexisting.ui.bar import Beside, progress_bar
from timeexisting.ui.theme import load_theme

THEME = load_theme()

_WIDTH = 40
_THIN = "━"
_THIN_TRACK = "─"
_LARGE = "█"
_LARGE_TRACK = "░"


def _plain(renderable, width: int = _WIDTH) -> list[str]:
    """Rendered lines without colour: the unfilled part of a bar is not drawn."""
    console = Console(file=StringIO(), width=width, color_system=None, legacy_windows=False)
    console.print(renderable)
    return console.file.getvalue().rstrip("\n").split("\n")


def _styles(renderable) -> set[Style]:
    console = Console(
        file=StringIO(), width=_WIDTH, color_system="truecolor", force_terminal=True, legacy_windows=False
    )
    return {segment.style for segment in console.render(renderable) if segment.style and segment.text.strip()}


@pytest.mark.parametrize(("fraction", "filled"), [(0.0, 0), (0.5, _WIDTH // 2), (1.0, _WIDTH)])
def test_thin_bar_fills_in_proportion_to_the_fraction(fraction, filled):
    lines = _plain(progress_bar(fraction, THEME.muted, THEME.bar_track))
    assert len(lines) == 1
    assert lines[0].count(_THIN) == filled


@pytest.mark.parametrize(("fraction", "filled"), [(0.0, 0), (0.5, _WIDTH // 2), (1.0, _WIDTH)])
def test_large_bar_fills_in_proportion_to_the_fraction(fraction, filled):
    lines = _plain(progress_bar(fraction, THEME.progress_mid, THEME.bar_track, large=True))
    assert len(lines) == 1
    assert lines[0].count(_LARGE) == filled


@pytest.mark.parametrize(("fraction", "filled"), [(-1.0, 0), (2.0, _WIDTH)])
def test_fraction_is_clamped_to_the_unit_interval(fraction, filled):
    assert _plain(progress_bar(fraction, THEME.muted, THEME.bar_track))[0].count(_THIN) == filled
    assert (
        _plain(progress_bar(fraction, THEME.progress_mid, THEME.bar_track, large=True))[0].count(_LARGE)
        == filled
    )


def test_thin_bar_draws_only_in_the_colours_it_was_given():
    bar = progress_bar(0.5, THEME.muted, THEME.bar_track)
    assert _styles(bar) == {Style.parse(THEME.muted), Style.parse(THEME.bar_track)}


def test_thin_bar_at_the_ends_draws_one_colour():
    assert _styles(progress_bar(0.0, THEME.muted, THEME.bar_track)) == {Style.parse(THEME.bar_track)}
    assert _styles(progress_bar(1.0, THEME.muted, THEME.bar_track)) == {Style.parse(THEME.muted)}


def test_beside_puts_the_bar_on_the_same_line_as_its_label():
    lines = _plain(Beside("50.0%", progress_bar(1.0, THEME.muted, THEME.bar_track)))
    assert len(lines) == 1
    assert lines[0].startswith("50.0% " + _THIN)
    assert lines[0].count(_THIN) == _WIDTH - len("50.0% ")


def test_beside_label_width_aligns_bars_on_neighbouring_rows():
    bar = progress_bar(1.0, THEME.muted, THEME.bar_track)
    short = _plain(Beside("1%", bar, label_width=10))[0]
    long = _plain(Beside("1234567890", bar, label_width=10))[0]
    assert short.index(_THIN) == long.index(_THIN) == 11


def test_beside_moves_the_bar_to_its_own_line_when_there_is_no_room():
    label = "September - 100.0%"
    lines = _plain(Beside(label, progress_bar(1.0, THEME.muted, THEME.bar_track)), width=20)
    assert lines == [label, _THIN * 20]


@pytest.mark.parametrize("no_color", [False, True])
@pytest.mark.parametrize(("fraction", "filled"), [(0.0, 0), (0.5, _WIDTH // 2), (1.0, _WIDTH)])
def test_whole_bar_is_visible_without_relying_on_colour(fraction, filled, no_color):
    """The unfilled part is drawn with its own glyph, not only in its own colour.

    Regression: the track was a dark grey that vanished on a dark terminal
    background, and was not drawn at all with colour off, so only the filled
    part of a bar could be seen.
    """
    console = Console(
        file=StringIO(),
        width=_WIDTH,
        color_system="truecolor",
        force_terminal=True,
        legacy_windows=False,
        no_color=no_color,
        record=True,
    )
    for large, glyph, track_glyph in ((False, _THIN, _THIN_TRACK), (True, _LARGE, _LARGE_TRACK)):
        console.print(progress_bar(fraction, THEME.muted, THEME.bar_track, large=large))
        (line,) = console.export_text().rstrip("\n").split("\n")
        assert line == glyph * filled + track_glyph * (_WIDTH - filled)
