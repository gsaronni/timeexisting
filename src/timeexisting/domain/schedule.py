"""Builds a day's segments from its start time, flags and configuration.

Pure: `(day, start, flags, cfg) -> DayPlan`. No clock, no I/O, no rich. There
are no scheduled breaks in this model: lunch is the only interruption to
working time, and only on a day that isn't flagged `no_lunch`.

Segments are aware datetimes in `cfg.display.timezone`, built by wall-clock
combination (`datetime.combine(day, moment, tzinfo=zone)`) rather than by
converting from UTC, so a segment's clock-face time never shifts around a
DST transition. `domain/resolver.py` compares these directly against the
clock's aware UTC `now`; Python resolves the two awares to the same instant
without either side needing to localize first.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from timeexisting.config.models import Config
from timeexisting.domain.segments import Phase, Segment


class DayFlag(StrEnum):
    NO_LUNCH = "no_lunch"
    HALF_DAY = "half_day"
    SICK = "sick"
    OFFSITE = "offsite"
    AFSPADSERING = "afspadsering"
    FRI = "fri"


# These flags replace the working-day shape outright with a single OFF_DAY
# segment; `no_lunch` and `half_day` instead modify that shape.
_OFF_DAY_FLAGS = frozenset({DayFlag.SICK, DayFlag.OFFSITE, DayFlag.AFSPADSERING, DayFlag.FRI})


@dataclass(frozen=True, slots=True)
class DayPlan:
    day: date
    segments: tuple[Segment, ...]
    start: datetime
    nominal_end: datetime
    target: timedelta
    deduction: timedelta
    flags: frozenset[DayFlag]


def _at(day: date, moment: time, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, moment, tzinfo=zone)


def _off_day_plan(day: date, flags: frozenset[DayFlag], label: str, zone: ZoneInfo) -> DayPlan:
    midnight = _at(day, time(), zone)
    next_midnight = _at(day + timedelta(days=1), time(), zone)
    segment = Segment(start=midnight, end=next_midnight, phase=Phase.OFF_DAY, label=label, paid=False)
    return DayPlan(
        day=day,
        segments=(segment,),
        start=midnight,
        nominal_end=midnight,
        target=timedelta(0),
        deduction=timedelta(0),
        flags=flags,
    )


def build_day(day: date, start: time, flags: frozenset[DayFlag], cfg: Config) -> DayPlan:
    zone = ZoneInfo(cfg.display.timezone)

    off_day_flag = next((flag for flag in flags if flag in _OFF_DAY_FLAGS), None)
    if off_day_flag is not None:
        return _off_day_plan(day, flags, off_day_flag.value, zone)
    if day.weekday() >= 5:
        return _off_day_plan(day, flags, "weekend", zone)

    no_lunch = DayFlag.NO_LUNCH in flags
    half_day = DayFlag.HALF_DAY in flags

    target = cfg.contract.daily_target / 2 if half_day else cfg.contract.daily_target
    deduction = timedelta(0) if no_lunch else cfg.lunch.deduction

    start_dt = _at(day, start, zone)
    nominal_end_dt = start_dt + target + deduction
    night_end_dt = _at(day, cfg.credit.night_end, zone)
    night_start_dt = _at(day, cfg.credit.night_start, zone)
    midnight = _at(day, time(), zone)
    next_midnight = _at(day + timedelta(days=1), time(), zone)

    segments: list[Segment] = [
        Segment(midnight, night_end_dt, Phase.OFF_HOURS, "off_hours", False),
        Segment(night_end_dt, start_dt, Phase.PRE_WORK, "pre_work", False),
    ]

    if no_lunch:
        segments.append(Segment(start_dt, nominal_end_dt, Phase.WORKING, "working", True))
    else:
        lunch_start_dt = _at(day, cfg.lunch.window_start, zone)
        lunch_end_dt = _at(day, cfg.lunch.window_end, zone)
        segments.append(Segment(start_dt, lunch_start_dt, Phase.WORKING, "working", True))
        segments.append(Segment(lunch_start_dt, lunch_end_dt, Phase.LUNCH, "lunch", False))
        segments.append(Segment(lunch_end_dt, nominal_end_dt, Phase.WORKING, "working", True))

    segments.append(Segment(nominal_end_dt, night_start_dt, Phase.POST_WORK, "post_work", False))
    segments.append(Segment(night_start_dt, next_midnight, Phase.OFF_HOURS, "off_hours", False))

    return DayPlan(
        day=day,
        segments=tuple(segments),
        start=start_dt,
        nominal_end=nominal_end_dt,
        target=target,
        deduction=deduction,
        flags=flags,
    )
