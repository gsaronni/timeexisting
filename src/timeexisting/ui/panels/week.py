"""The week panel: expected progress through the 37h working week.

Computed from the schedule, not the calendar: completed working days times
the daily target, plus today's scheduled elapsed working time (already
resolved), over the weekly target. Monday at 10:00 is no longer 0.0%.

Weekday comments may carry `{remaining_week}`, the planned working time left
in the ISO week from `domain/week.py`, formatted here as `33h18m`.
"""

from datetime import timedelta

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from timeexisting.config.models import Config
from timeexisting.content.phrases import pick
from timeexisting.domain.resolver import Resolved
from timeexisting.domain.segments import Phase
from timeexisting.domain.week import remaining_week, working_weekdays
from timeexisting.ui.bar import Beside, progress_bar
from timeexisting.ui.theme import Theme


def _is_off_day(resolved: Resolved) -> bool:
    segments = resolved.plan.segments
    return len(segments) == 1 and segments[0].phase is Phase.OFF_DAY


def _hours_minutes(span: timedelta) -> str:
    hours, minutes = divmod(int(span.total_seconds()) // 60, 60)
    return f"{hours}h{minutes:02d}m"


def _week_progress(resolved: Resolved, cfg: Config) -> float:
    working_indices = working_weekdays(cfg)
    today_index = resolved.plan.day.weekday()
    completed_days = sum(1 for index in working_indices if index < today_index)

    accrued = completed_days * cfg.contract.daily_target + resolved.expected_credit
    if cfg.contract.weekly_target.total_seconds() <= 0:
        return 0.0
    return max(0.0, min(1.0, accrued / cfg.contract.weekly_target))


def render(resolved: Resolved, cfg: Config, theme: Theme) -> Panel:
    day = resolved.plan.day
    weekday = day.weekday()
    weekday_name = day.strftime("%A")
    color = theme.weekday.get(weekday, theme.muted)
    comment = pick(f"weekday.{weekday}").format(remaining_week=_hours_minutes(remaining_week(resolved, cfg)))

    table = Table(box=box.ROUNDED, show_header=False, expand=True)
    table.add_column("Info", style=theme.muted)
    table.add_column("Value")

    if _is_off_day(resolved):
        status = pick("week.weekend_status")
    else:
        fraction = _week_progress(resolved, cfg)
        status = Beside(
            pick("week.workday_status").format(percent=round(fraction * 100, 1)),
            progress_bar(fraction, theme.muted, theme.bar_track),
        )
    table.add_row(weekday_name, status)
    table.add_row("Comment", Text(comment, style=theme.emphasis))

    footer_text = ""
    if weekday < 4:  # Monday .. Thursday
        days_left = 4 - weekday
        footer_text = pick("week.footer.countdown").format(days_left=days_left)
    elif weekday == 5:  # Saturday
        footer_text = pick("week.footer.saturday")
    elif weekday == 6:  # Sunday
        footer_text = pick("week.footer.sunday")

    footer = Text(footer_text, style=theme.footer, justify="center") if footer_text else None

    return Panel(
        table,
        title=pick("week.panel_title"),
        border_style=color,
        expand=True,
        subtitle=footer,
    )
