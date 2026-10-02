"""The one progress-bar renderer: every bar on the dashboard comes from here.

A bar is a fraction from 0 to 1 and the colours to draw it in, nothing else.
It does not know whether the fraction is calendar time, scheduled time or
hours worked, so a panel can change what it measures without touching this
module. Colours are passed in by the caller from `ui/theme.py`.

Two sizes, one renderer: the thin bar is a one-line rule that sits next to a
percentage; the large bar is the block bar of the day panel. Either way the
unfilled part is drawn with a glyph of its own, lighter than the filled one,
so the full length of a bar shows whatever the fraction, on any terminal
background, and with colour switched off.
"""

from rich.cells import cell_len
from rich.console import Console, ConsoleOptions, RenderableType, RenderResult
from rich.measure import Measurement
from rich.segment import Segment
from rich.table import Table
from rich.text import Text

# Narrower than this beside its label, a bar goes on a line of its own instead.
_MIN_BAR_WIDTH = 8

# (filled, unfilled) glyphs. The second pair of each is for consoles that
# cannot be trusted with box-drawing characters.
_THIN_GLYPHS = ("━", "─")
_THIN_GLYPHS_ASCII = ("=", "-")
_LARGE_GLYPHS = ("█", "░")
_LARGE_GLYPHS_ASCII = ("#", "-")


class ProgressBar:
    """One line: `fraction` of the available width filled, the rest track."""

    def __init__(self, fraction: float, color: str, track: str, *, large: bool = False) -> None:
        self.fraction = max(0.0, min(1.0, fraction))
        self.color = color
        self.track = track
        self.large = large

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        if options.ascii_only or options.legacy_windows:
            filled_glyph, track_glyph = _LARGE_GLYPHS_ASCII if self.large else _THIN_GLYPHS_ASCII
        else:
            filled_glyph, track_glyph = _LARGE_GLYPHS if self.large else _THIN_GLYPHS
        width = options.max_width
        # Floored: a bar is full only when the fraction really is 1.
        filled = int(self.fraction * width)
        if filled:
            yield Segment(filled_glyph * filled, console.get_style(self.color))
        if filled < width:
            yield Segment(track_glyph * (width - filled), console.get_style(self.track))
        yield Segment.line()

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement(min(_MIN_BAR_WIDTH, options.max_width), options.max_width)


def progress_bar(fraction: float, color: str, track: str, *, large: bool = False) -> RenderableType:
    """`color` draws the filled part, `track` the unfilled part."""
    return ProgressBar(fraction, color, track, large=large)


class Beside:
    """A label with a bar on the same line, the bar filling the rest of it.

    The bar's start column and length depend only on `label_width` and the
    space available, never on the label's own text, so rows that share a
    `label_width` get identical bars. Where the space left beside the label
    is under `_MIN_BAR_WIDTH`, the bar moves to its own line at full width:
    still the same start and length on every such row.
    """

    def __init__(self, label: str, bar: RenderableType, *, label_width: int | None = None) -> None:
        self.label = label
        self.bar = bar
        self.label_width = max(cell_len(label), label_width or 0)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        bar_width = options.max_width - self.label_width - 1
        if bar_width < _MIN_BAR_WIDTH:
            yield Text(self.label)
            yield self.bar
            return
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=self.label_width, no_wrap=True)
        grid.add_column(width=bar_width)
        grid.add_row(self.label, self.bar)
        yield grid

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        minimum = max(Measurement.get(console, options, Text(self.label)).minimum, _MIN_BAR_WIDTH)
        return Measurement(min(minimum, options.max_width), options.max_width)
