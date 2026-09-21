"""The layout: split once, then only panel contents change per tick.

Legacy rebuilt and re-split the whole `Layout` tree every frame (defect 6).
This module owns the one-time split; `ui/app.py` mutates panel contents
inside the already-built regions on every tick.
"""

from rich.layout import Layout

from timeexisting.content.art import load_art
from timeexisting.ui.panels import header
from timeexisting.ui.theme import Theme


def build_layout(theme: Theme) -> Layout:
    header_art = load_art("header")
    header_lines = header_art.count("\n") + 1 if header_art else 1
    header_size = header_lines + 4

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=header_size),
        Layout(name="main", ratio=1),
        Layout(name="footer", size=1),
    )
    layout["main"].split_row(
        Layout(name="left", ratio=1),
        Layout(name="right", ratio=1),
    )
    layout["main"]["left"].split_column(
        Layout(name="year"),
        Layout(name="week"),
    )

    layout["header"].update(header.render(theme))

    return layout
