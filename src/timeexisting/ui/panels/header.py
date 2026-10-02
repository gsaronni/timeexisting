"""The header panel: ASCII art banner plus the program's subtitle."""

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from timeexisting.content.art import load_art
from timeexisting.content.phrases import pick
from timeexisting.ui.art import centered_art_block
from timeexisting.ui.theme import Theme


def render(theme: Theme) -> Panel:
    art = load_art("header")
    subtitle = pick("header.subtitle")

    content = Table.grid()
    if art:
        content.add_row(centered_art_block(art, theme.header_text))
        content.add_row("")
    content.add_row(Text(subtitle, style=theme.header_subtitle, justify="center"))

    return Panel(content, box=box.DOUBLE, border_style=theme.header_border, expand=True)
