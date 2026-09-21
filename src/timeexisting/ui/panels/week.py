"""The week panel: progress through the working week."""

from datetime import datetime

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from timeexisting.content.phrases import pick
from timeexisting.ui.theme import Theme

_WORKING_DAYS = 5


def render(now: datetime, theme: Theme) -> Panel:
    weekday = now.weekday()  # Monday = 0 .. Sunday = 6
    weekday_name = now.strftime("%A")
    color = theme.weekday.get(weekday, theme.muted)
    comment = pick(f"weekday.{weekday}")

    table = Table(box=box.ROUNDED, show_header=False, expand=True)
    table.add_column("Info", style=theme.muted)
    table.add_column("Value")

    if weekday < _WORKING_DAYS:
        percent = round(weekday / _WORKING_DAYS * 100, 1)
        status = pick("week.workday_status").format(percent=percent)
    else:
        status = pick("week.weekend_status")
    table.add_row(weekday_name, status)
    table.add_row("Comment", Text(comment, style=theme.emphasis))

    footer_text = ""
    if weekday < _WORKING_DAYS - 1:  # Monday .. Thursday
        days_left = _WORKING_DAYS - 1 - weekday
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
