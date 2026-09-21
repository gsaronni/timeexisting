"""Frozen dataclasses for every table in roadmap section 15, plus `[power]`
from section 10 (loaded but unused until phase 5).

Durations are `timedelta`, parsed from strings like `"37h00m"` or `"30s"` by
`parse_duration`, the one helper for every duration field. Clock times come
back from `tomllib` as native `datetime.time` values already; `parse_clock_time`
is the one helper that validates a loaded value actually is one.
"""

import re
from dataclasses import dataclass
from datetime import time, timedelta

_DURATION_PATTERN = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


class ConfigError(Exception):
    """Anything wrong with a loaded configuration: an unknown key, a
    malformed duration, a value of the wrong shape.
    """


def parse_duration(value: object) -> timedelta:
    if not isinstance(value, str):
        raise ConfigError(f"duration must be a string like '30m': {value!r}")
    match = _DURATION_PATTERN.match(value)
    if match is None or not any(match.groups()):
        raise ConfigError(f"not a valid duration: {value!r}")
    hours, minutes, seconds = (int(group) if group else 0 for group in match.groups())
    return timedelta(hours=hours, minutes=minutes, seconds=seconds)


def parse_clock_time(value: object) -> time:
    if not isinstance(value, time):
        raise ConfigError(f"not a valid clock time: {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class ContractConfig:
    weekly_target: timedelta
    daily_target: timedelta
    working_days: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LunchConfig:
    window_start: time
    window_end: time
    deduction: timedelta
    min_minutes: int


@dataclass(frozen=True, slots=True)
class CreditConfig:
    earliest: time
    latest: time
    night_start: time
    night_end: time
    default_start: time


@dataclass(frozen=True, slots=True)
class AbsenceConfig:
    micro_max: timedelta
    meeting_slots: tuple[timedelta, ...]
    meeting_tolerance: timedelta
    ask_above: timedelta


@dataclass(frozen=True, slots=True)
class DriftConfig:
    green_max: timedelta
    amber_max: timedelta


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    heartbeat: timedelta
    poll: timedelta


@dataclass(frozen=True, slots=True)
class NotifyConfig:
    kitchen_start: time
    kitchen_repeat: time
    kitchen_end: time


@dataclass(frozen=True, slots=True)
class DisplayConfig:
    timezone: str
    refresh_hz: float
    voice: str
    locale: str
    palette: str


@dataclass(frozen=True, slots=True)
class ProfilesConfig:
    default: str
    hosts: dict[str, str]


@dataclass(frozen=True, slots=True)
class PowerConfig:
    action: str
    trigger: str
    warning: timedelta
    postpone: timedelta
    postpone_max: int
    weekends: bool
    holidays: bool
    sick: bool


@dataclass(frozen=True, slots=True)
class Config:
    version: int
    contract: ContractConfig
    lunch: LunchConfig
    credit: CreditConfig
    absence: AbsenceConfig
    drift: DriftConfig
    collector: CollectorConfig
    notify: NotifyConfig
    display: DisplayConfig
    profiles: ProfilesConfig
    power: PowerConfig
