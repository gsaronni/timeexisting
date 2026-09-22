"""The Live render loop and clean shutdown.

Naive `datetime.now()` calls (defect 13) are gone: `clock.now()` is always
an aware UTC call, converted to Europe/Copenhagen once, here, at the render
boundary. The loop runs one cadence (defect 7): `refresh_hz` drives both the
Live repaint rate and the content-update sleep. On Ctrl+C, `Live`'s context
manager stops the display before the `except` block prints the exit line;
nothing sleeps on the way out (defect 11).

Each tick builds the day's `DayPlan` from the (already-localized) date and
resolves it against the (already-localized) instant, so `Resolved.now` is
directly usable by the panels without further conversion.
"""

import time
from collections.abc import Sequence
from datetime import datetime
from datetime import time as day_time
from functools import cache
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.text import Text

from timeexisting.config.models import Config
from timeexisting.content.phrases import pick
from timeexisting.domain.clock import Clock
from timeexisting.domain.resolver import Resolved, resolve
from timeexisting.domain.schedule import DayFlag, build_day
from timeexisting.ui.layout import build_layout
from timeexisting.ui.panels import day, week, year
from timeexisting.ui.theme import Theme, load_theme

TIMEZONE_NAME = "Europe/Copenhagen"


@cache
def local_timezone() -> ZoneInfo:
    return ZoneInfo(TIMEZONE_NAME)


def _localize(now_utc: datetime) -> datetime:
    return now_utc.astimezone(local_timezone())


def _update(layout: Layout, resolved: Resolved, cfg: Config, theme: Theme) -> None:
    layout["main"]["left"]["year"].update(year.render(resolved.now, theme))
    layout["main"]["left"]["week"].update(week.render(resolved, cfg, theme))
    layout["main"]["right"].update(day.render(resolved, theme))


def run(
    clock: Clock,
    cfg: Config,
    start: day_time,
    flags: frozenset[DayFlag],
    *,
    refresh_hz: float = 1.0,
    label: str = "",
    console: Console | None = None,
) -> None:
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
                now_local = _localize(clock.now())
                plan = build_day(now_local.date(), start, flags, cfg)
                resolved = resolve(now_local, plan)
                _update(layout, resolved, cfg, theme)
                time.sleep(interval)
    except KeyboardInterrupt:
        console.print(pick("app.interrupted"), style=theme.danger)


def run_demo(
    scenarios: Sequence[tuple[str, Resolved]],
    cfg: Config,
    *,
    step_seconds: float = 4.0,
    refresh_hz: float = 1.0,
    console: Console | None = None,
) -> None:
    """Cycle the panels through a fixed list of `(label, resolved)` scenarios,
    each held for `step_seconds` before advancing, looping forever. Every
    scenario is a precomputed `Resolved` at a fixed moment: this is a
    showcase of states, not an accelerated clock.
    """
    theme = load_theme()
    layout = build_layout(theme)
    console = console or Console()

    console.print(pick("app.starting"), style=theme.muted)

    interval = 1.0 / refresh_hz
    ticks_per_step = max(1, round(step_seconds / interval))
    demo_tag = pick("app.demo_tag")

    try:
        with Live(layout, console=console, refresh_per_second=refresh_hz, screen=True):
            while True:
                for label, resolved in scenarios:
                    footer_text = f"{label} {demo_tag}" if demo_tag else label
                    layout["footer"].update(Text(footer_text, style=theme.muted, justify="center"))
                    for _ in range(ticks_per_step):
                        _update(layout, resolved, cfg, theme)
                        time.sleep(interval)
    except KeyboardInterrupt:
        console.print(pick("app.interrupted"), style=theme.danger)
