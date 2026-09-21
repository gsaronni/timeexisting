"""The Live render loop and clean shutdown.

Naive `datetime.now()` calls (defect 13) are gone: `clock.now()` is always
an aware UTC call, converted to Europe/Copenhagen once, here, at the render
boundary. The loop runs one cadence (defect 7): `refresh_hz` drives both the
Live repaint rate and the content-update sleep. On Ctrl+C, `Live`'s context
manager stops the display before the `except` block prints the exit line;
nothing sleeps on the way out (defect 11).
"""

import time
from datetime import datetime
from functools import cache
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.text import Text

from timeexisting.content.phrases import pick
from timeexisting.domain.clock import Clock
from timeexisting.ui.layout import build_layout
from timeexisting.ui.panels import day, week, year
from timeexisting.ui.theme import Theme, load_theme

TIMEZONE_NAME = "Europe/Copenhagen"


@cache
def local_timezone() -> ZoneInfo:
    return ZoneInfo(TIMEZONE_NAME)


def _localize(now_utc: datetime) -> datetime:
    return now_utc.astimezone(local_timezone())


def _update(layout: Layout, now: datetime, theme: Theme) -> None:
    layout["main"]["left"]["year"].update(year.render(now, theme))
    layout["main"]["left"]["week"].update(week.render(now, theme))
    layout["main"]["right"].update(day.render(now, theme))


def run(clock: Clock, *, refresh_hz: float = 1.0, label: str = "", console: Console | None = None) -> None:
    theme = load_theme()
    layout = build_layout(theme)
    console = console or Console()

    footer_text = pick("app.footer")
    if label:
        footer_text = f"{footer_text} [{label}]"
    layout["footer"].update(Text(footer_text, style=theme.muted, justify="center"))

    console.print(pick("app.starting"), style=theme.muted)

    interval = 1.0 / refresh_hz
    try:
        with Live(layout, console=console, refresh_per_second=refresh_hz, screen=True):
            while True:
                _update(layout, _localize(clock.now()), theme)
                time.sleep(interval)
    except KeyboardInterrupt:
        console.print(pick("app.interrupted"), style=theme.danger)
