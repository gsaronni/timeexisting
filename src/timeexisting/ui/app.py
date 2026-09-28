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

The footer's second line reports the collector. The viewer is handed a
callable returning the live collector's start time, or `None`, and asks it
once per tick, so a collector that dies or appears while the dashboard is
open is reported without a restart.
"""

import time
from collections.abc import Callable, Sequence
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


def _collector_line(since: datetime | None, spawn_failed: bool, theme: Theme) -> Text:
    if since is not None:
        text = pick("app.collector.alive").format(since=f"{_localize(since):%H:%M}")
        return Text(text, style=theme.collector_alive, justify="center")
    key = "app.collector.spawn_failed" if spawn_failed else "app.collector.absent"
    return Text(pick(key), style=theme.collector_absent, justify="center")


def _footer(text: str, collector: Text | None, theme: Theme) -> Text:
    footer = Text(text, style=theme.muted, justify="center")
    if collector is not None:
        footer.append("\n")
        footer.append_text(collector)
    return footer


def run(
    clock: Clock,
    cfg: Config,
    start: day_time,
    flags: frozenset[DayFlag],
    *,
    refresh_hz: float = 1.0,
    label: str = "",
    console: Console | None = None,
    collector_since: Callable[[], datetime | None] | None = None,
    spawn_failed: bool = False,
) -> None:
    """`collector_since` returns the live collector's start time, or `None` when none runs; when it is itself `None` the footer carries no collector line. `spawn_failed` marks a collector the viewer tried to start that never took its lock."""
    theme = load_theme()
    layout = build_layout(theme)
    console = console or Console()

    footer_text = pick("app.footer")
    if label:
        footer_text = f"{footer_text} [{label}]"

    console.print(pick("app.starting"), style=theme.muted)

    interval = 1.0 / refresh_hz
    try:
        with Live(layout, console=console, refresh_per_second=refresh_hz, screen=True):
            while True:
                collector = None
                if collector_since is not None:
                    since = collector_since()
                    # Once a collector has been seen, a later absence is a stop, not a failed spawn.
                    spawn_failed = spawn_failed and since is None
                    collector = _collector_line(since, spawn_failed, theme)
                layout["footer"].update(_footer(footer_text, collector, theme))
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
