"""The day panel: renders from a resolved day only, by `match` on phase.

Every value shown comes from `Resolved` (which itself carries `now` and the
built `DayPlan`): no clock, no schedule arithmetic happens here. `Resolved`
replaces the legacy `make_time_panel` chain of `if`/`elif` branches (defect
1: a misindented block let a pre-work run fall through into the work-hours
arithmetic and produce negative durations) with a `match` on the resolved
`Phase`, so each branch is reachable only when the phase actually says so.
"""

from rich.bar import Bar
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from timeexisting.content.art import load_art
from timeexisting.content.phrases import pick
from timeexisting.domain.resolver import Resolved
from timeexisting.domain.segments import Phase, Segment
from timeexisting.ui.theme import Theme

_ANIMATION_FRAMES = ("|", "/", "-", "\\")

_WEEKEND_ACTIVITIES = ("gaming", "intimate", "reading", "sleep", "surf", "mtb", "fencing", "chess")
_WEEKEND_ACTIVITY_PERIOD_SECONDS = 30

# Off-day flags with dedicated content; the rest (weekend, offsite, fri)
# fall back to the weekend activity pool for now.
_OFF_DAY_DEDICATED_LABELS = frozenset({"sick", "afspadsering"})


def render(resolved: Resolved, theme: Theme) -> Panel:
    match resolved.segment.phase:
        case Phase.OFF_DAY:
            return _render_off_day(resolved, theme)
        case Phase.OFF_HOURS:
            return _render_off_hours(resolved, theme)
        case Phase.PRE_WORK:
            return _render_pre_work(resolved, theme)
        case Phase.WORKING:
            return _render_working(resolved, theme)
        case Phase.LUNCH:
            return _render_lunch(resolved, theme)
        case Phase.POST_WORK:
            return _render_post_work(resolved, theme)


def _current_time_line(resolved: Resolved, theme: Theme) -> Text:
    template = pick("common.current_time")
    return Text(template.format(time=resolved.now.strftime("%H:%M:%S")), style=theme.clock, justify="center")


def _segment_label(segment: Segment) -> str:
    return pick(f"{segment.label}.label")


def _render_off_day(resolved: Resolved, theme: Theme) -> Panel:
    label = resolved.segment.label
    if label in _OFF_DAY_DEDICATED_LABELS:
        heading = pick(f"{label}.heading")
        message = pick(f"{label}.message")
        art = None
    else:
        index = int(resolved.now.timestamp()) // _WEEKEND_ACTIVITY_PERIOD_SECONDS % len(_WEEKEND_ACTIVITIES)
        activity = _WEEKEND_ACTIVITIES[index]
        art = load_art(activity)
        heading = pick("weekend.heading")
        message = pick(f"weekend_activity.{activity}.message")

    content = Table.grid(padding=1)
    content.add_row(Text(heading, style=theme.weekend_title, justify="center"))
    content.add_row(Text(message, style=theme.weekend_message, justify="center"))
    if art:
        content.add_row(Text(art, style=theme.muted))
    content.add_row(Text(pick("weekend.footer"), style=theme.footer, justify="center"))
    content.add_row(_current_time_line(resolved, theme))

    return Panel(
        content,
        title=pick("weekend.panel_title"),
        border_style=theme.phase["weekend"],
        expand=True,
    )


def _render_off_hours(resolved: Resolved, theme: Theme) -> Panel:
    content = Table.grid(padding=1)
    content.add_row(Text(pick("off_hours.heading"), style=theme.muted, justify="center"))
    content.add_row(Text(pick("off_hours.message"), style=theme.muted, justify="center"))
    content.add_row(_current_time_line(resolved, theme))

    return Panel(content, title=pick("off_hours.panel_title"), border_style=theme.muted, expand=True)


def _render_pre_work(resolved: Resolved, theme: Theme) -> Panel:
    hours, rest = divmod(int(resolved.remaining.total_seconds()), 3600)
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
    content.add_row(_current_time_line(resolved, theme))

    return Panel(
        content, title=pick("pre_work.panel_title"), border_style=theme.phase["pre_work"], expand=True
    )


def _render_post_work(resolved: Resolved, theme: Theme) -> Panel:
    art = load_art("sunset")
    message = pick("post_work.message")
    completed_template = pick("post_work.completed_at")

    content = Table.grid(padding=1)
    if art:
        content.add_row(Text(art, style=theme.muted))
    content.add_row(Text(message, style=theme.post_work_message, justify="center"))
    content.add_row(
        Text(
            completed_template.format(time=resolved.plan.nominal_end.strftime("%H:%M")),
            style=theme.phase["post_work"],
            justify="center",
        )
    )
    content.add_row(_current_time_line(resolved, theme))

    return Panel(
        content, title=pick("post_work.panel_title"), border_style=theme.phase["post_work"], expand=True
    )


def _render_lunch(resolved: Resolved, theme: Theme) -> Panel:
    remaining_minutes = int(resolved.remaining.total_seconds() // 60)
    color = theme.break_type.get("lunch", theme.muted)
    art = load_art("lunch")
    heading = pick("lunch.heading")
    message = pick("lunch.message")

    content = Table.grid(padding=1)
    content.add_row(Text(heading, style=theme.bold(color), justify="center"))
    content.add_row(Text(message, style=theme.emphasis, justify="center"))
    if art:
        content.add_row(Text(art, style=theme.muted))
    template = pick("lunch.time_remaining")
    content.add_row(Text(template.format(minutes=remaining_minutes), style=theme.countdown, justify="center"))
    content.add_row(_current_time_line(resolved, theme))

    return Panel(content, title=pick("lunch.panel_title"), border_style=color, expand=True)


def _render_working(resolved: Resolved, theme: Theme) -> Panel:
    plan = resolved.plan
    hours, rest = divmod(int(resolved.remaining_to_end.total_seconds()), 3600)
    minutes, seconds = divmod(rest, 60)

    # Floored, not rounded: 100% appears only once `progress` actually
    # reaches 1.0 at `nominal_end`, not a minute early.
    percentage = int(resolved.progress * 100)
    progress_color = theme.progress_color(percentage)
    anim = _ANIMATION_FRAMES[resolved.now.second % len(_ANIMATION_FRAMES)]
    message_bucket = min(percentage // 10, 9)
    message = pick(f"existential.{message_bucket}")

    content = Table.grid(padding=1)
    content.add_row(
        Text(
            pick("working.progress").format(percentage=percentage),
            style=theme.bold(progress_color),
            justify="center",
        )
    )
    content.add_row(Bar(size=1.0, begin=0.0, end=resolved.progress, color=progress_color))
    content.add_row(
        Text(
            pick("working.remaining").format(hh=hours, mm=minutes, ss=seconds),
            style=theme.countdown_emphasis,
            justify="center",
        )
    )
    content.add_row(
        Text(
            pick("working.window").format(
                start=plan.start.strftime("%H:%M"), nominal_end=plan.nominal_end.strftime("%H:%M")
            ),
            style=theme.countdown,
            justify="center",
        )
    )
    content.add_row(_current_time_line(resolved, theme))
    content.add_row(
        Text(
            pick("working.status").format(anim=anim, message=message), style=theme.emphasis, justify="center"
        )
    )

    if resolved.next_segment is not None:
        content.add_row(
            Text(
                pick("working.next_segment").format(
                    label=_segment_label(resolved.next_segment),
                    time=resolved.next_segment.start.strftime("%H:%M"),
                ),
                style=theme.next_break,
                justify="center",
            )
        )

    return Panel(content, title=pick("working.panel_title"), border_style=theme.phase["working"], expand=True)
