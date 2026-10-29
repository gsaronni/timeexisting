"""Builds a day's segments from its start time, flags and configuration.

Pure: `(day, start, flags, cfg) -> DayPlan`. No clock, no I/O, no rich. There
are no scheduled breaks in this model: lunch is the only interruption to
working time, and only on a day that isn't flagged `no_lunch`.

The paid segments between `start` and `nominal_end` add up to `target` for
every start, and the unpaid time between them is exactly `deduction`, so the
progress bar (credit over target) reaches 100% at `nominal_end` and never
before. The lunch window keeps its wall-clock place while it fits strictly
inside the working window, with working time on both sides of it; otherwise
it moves to the start, keeping its length. That covers a start at or after
the window's opening, and a day too short to reach the window before its
nominal end. Nothing paid ever opens before the start. The day's
night-window bookends yield to the paid shape rather than cutting it: a
nominal end past `night_start`, or past midnight, is still reached, and the
day's last segment ends there.

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


def _lunch(day: date, start: datetime, nominal_end: datetime, cfg: Config, zone: ZoneInfo) -> Segment:
    """The lunch window where it fits, else moved to the start, its length kept.

    It fits when working time precedes and follows it: the start before its opening, its close before the nominal end. A window that opened before the start, or that would close at or after the nominal end, is taken at the start instead, so the working block after it reaches the target exactly at the nominal end and not earlier."""
    window_start = _at(day, cfg.lunch.window_start, zone)
    length = _at(day, cfg.lunch.window_end, zone) - window_start
    fits = start < window_start and window_start + length < nominal_end
    lunch_start = window_start if fits else start
    return Segment(lunch_start, lunch_start + length, Phase.LUNCH, "lunch", False)


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
    midnight = _at(day, time(), zone)
    next_midnight = _at(day + timedelta(days=1), time(), zone)
    # The bookends never reach into the paid shape: pre-work opens no later
    # than the start, post-work closes no earlier than the nominal end.
    pre_work_start = min(_at(day, cfg.credit.night_end, zone), start_dt)
    post_work_end = max(_at(day, cfg.credit.night_start, zone), nominal_end_dt)
    day_end = max(next_midnight, nominal_end_dt)

    segments: list[Segment] = [
        Segment(midnight, pre_work_start, Phase.OFF_HOURS, "off_hours", False),
        Segment(pre_work_start, start_dt, Phase.PRE_WORK, "pre_work", False),
    ]

    if no_lunch:
        segments.append(Segment(start_dt, nominal_end_dt, Phase.WORKING, "working", True))
    else:
        lunch = _lunch(day, start_dt, nominal_end_dt, cfg, zone)
        segments.append(Segment(start_dt, lunch.start, Phase.WORKING, "working", True))
        segments.append(lunch)
        segments.append(Segment(lunch.end, nominal_end_dt, Phase.WORKING, "working", True))

    segments.append(Segment(nominal_end_dt, post_work_end, Phase.POST_WORK, "post_work", False))
    segments.append(Segment(post_work_end, day_end, Phase.OFF_HOURS, "off_hours", False))

    return DayPlan(
        day=day,
        # A clamp that meets its neighbour leaves an empty segment behind: a
        # morning block for a start at the lunch window, pre-work for a start
        # inside the night window, post-work for a nominal end past it.
        segments=tuple(segment for segment in segments if segment.start < segment.end),
        start=start_dt,
        nominal_end=nominal_end_dt,
        target=target,
        deduction=deduction,
        flags=flags,
    )
