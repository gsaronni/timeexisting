"""ASCII art as a renderable: one block, never justified line by line.

`Text(..., justify="center")` centres every line on its own after stripping
its trailing whitespace, so lines of different lengths land at different
offsets and the picture shears. Art is positioned as a whole instead: the
block keeps the file's leading whitespace on every line, is never wrapped
(a wrapped line would break the picture and the fixed header height), and
is centred, when it is centred, by one offset shared by all its lines.
"""

from rich.align import Align
from rich.text import Text


def art_block(art: str, style: str) -> Text:
    return Text(art, style=style, no_wrap=True, overflow="crop")


def centered_art_block(art: str, style: str) -> Align:
    return Align.center(art_block(art, style))
