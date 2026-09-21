"""The day panel: weekend vibes, breaks, pre/post-work, and the work clock.

Replaces the legacy `make_time_panel` chain of `if`/`elif` branches (defect
1: a misindented block let a pre-work run fall through into the work-hours
arithmetic and produce negative durations) with a `match` on the resolved
`Phase`, so each branch is reachable only when the phase actually says so.
"""

from datetime import datetime

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from timeexisting.content.art import load_art
from timeexisting.content.phrases import pick
from timeexisting.domain.phases import (
    END_HOUR,
    START_HOUR,
    Phase,
    next_break,
    resolve_break_progress,
    resolve_phase,
    resolve_work_progress,
)
from timeexisting.ui.theme import Theme

_ANIMATION_FRAMES = ("|", "/", "-", "\\")

_WEEKEND_ACTIVITIES = ("gaming", "intimate", "reading", "sleep", "surf", "mtb", "fencing", "chess")
_WEEKEND_ACTIVITY_PERIOD_SECONDS = 30

_BREAK_NAMES = {
    Phase.MORNING_BREAK: "morning",
    Phase.LUNCH_BREAK: "lunch",
    Phase.AFTERNOON_BREAK: "afternoon",
}
_BREAK_ART = {"morning": "break_morning", "lunch": "lunch", "afternoon": "break_afternoon"}


def render(now: datetime, theme: Theme) -> Panel:
    phase = resolve_phase(now)
    match phase:
        case Phase.WEEKEND:
            return _render_weekend(now, theme)
        case Phase.MORNING_BREAK | Phase.LUNCH_BREAK | Phase.AFTERNOON_BREAK:
            return _render_break(phase, now, theme)
        case Phase.PRE_WORK:
            return _render_pre_work(now, theme)
        case Phase.POST_WORK:
            return _render_post_work(now, theme)
        case Phase.WORK:
            return _render_work(now, theme)


def _current_time_line(now: datetime, theme: Theme) -> Text:
    template = pick("common.current_time")
    return Text(template.format(time=now.strftime("%H:%M:%S")), style=theme.clock, justify="center")


def _render_weekend(now: datetime, theme: Theme) -> Panel:
    index = int(now.timestamp()) // _WEEKEND_ACTIVITY_PERIOD_SECONDS % len(_WEEKEND_ACTIVITIES)
    activity = _WEEKEND_ACTIVITIES[index]
    art = load_art(activity)
    message = pick(f"weekend_activity.{activity}.message")

    content = Table.grid(padding=1)
    content.add_row(Text(pick("weekend.heading"), style=theme.weekend_title, justify="center"))
    content.add_row(Text(message, style=theme.weekend_message, justify="center"))
    if art:
        content.add_row(Text(art, style=theme.muted))
    content.add_row(Text(pick("weekend.footer"), style=theme.footer, justify="center"))
    content.add_row(_current_time_line(now, theme))

    return Panel(
        content,
        title=pick("weekend.panel_title"),
        border_style=theme.phase["weekend"],
        expand=True,
    )


def _render_break(phase: Phase, now: datetime, theme: Theme) -> Panel:
    name = _BREAK_NAMES[phase]
    color = theme.break_type.get(name, theme.muted)
    art = load_art(_BREAK_ART[name])
    message = pick(f"break.{name}.message")
    heading = pick(f"break.{name}.heading")
    remaining_minutes = int(resolve_break_progress(now).remaining.total_seconds() // 60)

    content = Table.grid(padding=1)
    content.add_row(Text(heading, style=f"bold {color}", justify="center"))
    content.add_row(Text(message, style=theme.emphasis, justify="center"))
    if art:
        content.add_row(Text(art, style=theme.muted))
    template = pick("break.time_remaining")
    content.add_row(Text(template.format(minutes=remaining_minutes), style=theme.countdown, justify="center"))
    content.add_row(_current_time_line(now, theme))

    return Panel(content, title=pick("break.panel_title"), border_style=color)


def _render_pre_work(now: datetime, theme: Theme) -> Panel:
    start_time = now.replace(hour=START_HOUR, minute=0, second=0, microsecond=0)
    remaining = start_time - now
    hours, rest = divmod(int(remaining.total_seconds()), 3600)
    minutes, seconds = divmod(rest, 60)

    art = load_art("coffee")
    message = pick("pre_work.message")
    countdown_template = pick("pre_work.countdown")

    content = Table.grid(padding=1)
    if art:
        content.add_row(Text(art, style=theme.muted))
    content.add_row(Text(message, style=theme.emphasis, justify="center"))
    content.add_row(
        Text(
            countdown_template.format(hh=hours, mm=minutes, ss=seconds),
            style=theme.countdown_emphasis,
            justify="center",
        )
    )
    content.add_row(_current_time_line(now, theme))

    return Panel(
        content, title=pick("pre_work.panel_title"), border_style=theme.phase["pre_work"], expand=True
    )


def _render_post_work(now: datetime, theme: Theme) -> Panel:
    art = load_art("sunset")
    message = pick("post_work.message")
    completed_template = pick("post_work.completed_at")

    content = Table.grid(padding=1)
    if art:
        content.add_row(Text(art, style=theme.muted))
    content.add_row(Text(message, style=theme.post_work_message, justify="center"))
    content.add_row(
        Text(completed_template.format(hh=END_HOUR), style=theme.phase["post_work"], justify="center")
    )
    content.add_row(_current_time_line(now, theme))

    return Panel(
        content, title=pick("post_work.panel_title"), border_style=theme.phase["post_work"], expand=True
    )


def _render_work(now: datetime, theme: Theme) -> Panel:
    progress = resolve_work_progress(now)
    hours, rest = divmod(int(progress.remaining.total_seconds()), 3600)
    minutes, seconds = divmod(rest, 60)

    anim = _ANIMATION_FRAMES[now.second % len(_ANIMATION_FRAMES)]
    message = pick(f"existential.{progress.message_bucket}")
    progress_color = theme.progress_color(progress.percentage)

    content = Table.grid(padding=1)
    content.add_row(
        Text(
            pick("work.progress").format(percentage=progress.percentage),
            style=f"bold {progress_color}",
            justify="center",
        )
    )
    content.add_row(
        Text(
            pick("work.remaining").format(hh=hours, mm=minutes, ss=seconds),
            style=theme.countdown_emphasis,
            justify="center",
        )
    )
    content.add_row(_current_time_line(now, theme))
    content.add_row(
        Text(pick("work.status").format(anim=anim, message=message), style=theme.emphasis, justify="center")
    )

    upcoming = next_break(now)
    if upcoming is not None:
        break_phase, minutes_until = upcoming
        break_name = _BREAK_NAMES[break_phase]
        label = pick(f"break.{break_name}.label")
        content.add_row(
            Text(
                pick("work.next_break").format(minutes=minutes_until, label=label),
                style=theme.next_break,
                justify="center",
            )
        )

    return Panel(content, title=pick("work.panel_title"), border_style=theme.phase["work"], expand=True)
