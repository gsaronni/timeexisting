"""Colour and style tokens.

Every colour string in the program lives here, loaded from
content/palettes/<name>.toml. Nothing outside this module names a colour.
"""

import tomllib
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

DEFAULT_PALETTE = "grimdark"

_PALETTES_DIR = "content/palettes"


@cache
def _load_palette(name: str) -> dict:
    path = files("timeexisting").joinpath(_PALETTES_DIR).joinpath(f"{name}.toml")
    if not path.is_file():
        path = files("timeexisting").joinpath(_PALETTES_DIR).joinpath(f"{DEFAULT_PALETTE}.toml")
    with path.open("rb") as handle:
        return tomllib.load(handle)


@dataclass(frozen=True, slots=True)
class Theme:
    header_border: str
    header_text: str
    header_subtitle: str
    muted: str
    emphasis: str
    year_footer: str
    footer: str
    weekend_title: str
    weekend_message: str
    clock: str
    countdown: str
    countdown_emphasis: str
    post_work_message: str
    next_break: str
    danger: str
    collector_alive: str
    collector_absent: str
    season: dict[str, str]
    weekday: dict[int, str]
    break_type: dict[str, str]
    phase: dict[str, str]
    progress_low: str
    progress_mid: str
    progress_high: str

    def progress_color(self, percentage: int) -> str:
        if percentage < 33:
            return self.progress_low
        if percentage < 66:
            return self.progress_mid
        return self.progress_high

    def bold(self, style: str) -> str:
        return f"bold {style}"


@cache
def load_theme(name: str = DEFAULT_PALETTE) -> Theme:
    data = _load_palette(name)
    roles = data.get("roles", {})
    progress = data.get("progress", {})
    weekday = {int(key): value for key, value in data.get("weekday", {}).items()}
    return Theme(
        header_border=roles.get("header_border", "white"),
        header_text=roles.get("header_text", "white"),
        header_subtitle=roles.get("header_subtitle", "bold white"),
        muted=roles.get("muted", "dim"),
        emphasis=roles.get("emphasis", "italic"),
        year_footer=roles.get("year_footer", "dim"),
        footer=roles.get("footer", "dim"),
        weekend_title=roles.get("weekend_title", "bold white"),
        weekend_message=roles.get("weekend_message", "italic"),
        clock=roles.get("clock", "white"),
        countdown=roles.get("countdown", "white"),
        countdown_emphasis=roles.get("countdown_emphasis", "bold white"),
        post_work_message=roles.get("post_work_message", "italic"),
        next_break=roles.get("next_break", "dim"),
        danger=roles.get("danger", "bold red"),
        collector_alive=roles.get("collector_alive", "dim green"),
        collector_absent=roles.get("collector_absent", "dim red"),
        season=dict(data.get("season", {})),
        weekday=weekday,
        break_type=dict(data.get("break", {})),
        phase=dict(data.get("phase", {})),
        progress_low=progress.get("low", "red"),
        progress_mid=progress.get("mid", "yellow"),
        progress_high=progress.get("high", "green"),
    )
