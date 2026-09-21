"""The year panel: annual and monthly progress, with a seasonal remark."""

from datetime import datetime

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from timeexisting.content.phrases import pick
from timeexisting.ui.theme import Theme

_SEASON_BY_MONTH = {
    12: "Winter",
    1: "Winter",
    2: "Winter",
    3: "Spring",
    4: "Spring",
    5: "Spring",
    6: "Summer",
    7: "Summer",
    8: "Summer",
    9: "Fall",
    10: "Fall",
    11: "Fall",
}


def _days_in_month(now: datetime) -> int:
    this_month = now.replace(day=1)
    if now.month == 12:
        next_month = now.replace(year=now.year + 1, month=1, day=1)
    else:
        next_month = now.replace(month=now.month + 1, day=1)
    return (next_month - this_month).days


def render(now: datetime, theme: Theme) -> Panel:
    start_of_year = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    end_of_year = now.replace(month=12, day=31, hour=0, minute=0, second=0, microsecond=0)
    total_days = (end_of_year - start_of_year).days + 1
    day_of_year = (now - start_of_year).days + 1

    days_in_month = _days_in_month(now)
    season = _SEASON_BY_MONTH[now.month]
    season_color = theme.season.get(season, theme.muted)

    table = Table(box=box.ROUNDED, show_header=False, expand=True)
    table.add_column("Info", style=theme.muted)
    table.add_column("Value")

    year_percent = round(day_of_year / total_days * 100, 1)
    table.add_row("Year Progress", f"{year_percent}%")
    table.add_row("", f"{day_of_year}/{total_days} days")

    month_percent = round(now.day / days_in_month * 100, 1)
    table.add_row("Month", f"{now.strftime('%B')} - {month_percent}%")
    table.add_row("", f"{now.day}/{days_in_month} days")

    table.add_row("Season", Text(season, style=season_color))
    table.add_row("Season Remark", Text(pick(f"season.{season}"), style=theme.emphasis))
    table.add_row("Month Remark", Text(pick(f"month.{now.month}"), style=theme.emphasis))

    days_left = total_days - day_of_year
    footer_template = pick("year.footer")
    footer = Text(footer_template.format(days_left=days_left), style=theme.year_footer, justify="center")

    return Panel(
        table,
        title=pick("year.panel_title"),
        border_style=season_color,
        expand=True,
        subtitle=footer,
    )
