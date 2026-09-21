"""The stopgap day-phase resolver.

This models the legacy 09:00-18:00 day with its three fixed breaks, as an
explicit, pure `(now) -> Phase` function. Phase 1 replaces this with the real
schedule builder and resolver; until then, this is what stands between the
program and defect 1 (the indentation bug that let a pre-work run fall
through into the work-hours branch and produce negative durations).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

START_HOUR = 9
END_HOUR = 18

# Break windows, minutes since midnight, half-open [start, end). Data, not
# code, so phase 1 can drop this table without touching the resolver shape.
_BREAK_WINDOWS: tuple[tuple[Phase, int, int], ...] = (
    ("morning_break", 11 * 60, 11 * 60 + 15),
    ("lunch_break", 13 * 60, 14 * 60),
    ("afternoon_break", 16 * 60, 16 * 60 + 15),
)


class Phase(StrEnum):
    PRE_WORK = "pre_work"
    MORNING_BREAK = "morning_break"
    LUNCH_BREAK = "lunch_break"
    AFTERNOON_BREAK = "afternoon_break"
    WORK = "work"
    POST_WORK = "post_work"
    WEEKEND = "weekend"


_BREAK_PHASES: tuple[tuple[Phase, int, int], ...] = tuple(
    (Phase(name), start, end) for name, start, end in _BREAK_WINDOWS
)


def resolve_phase(now: datetime) -> Phase:
    if now.weekday() >= 5:
        return Phase.WEEKEND

    minutes = now.hour * 60 + now.minute
    for phase, start, end in _BREAK_PHASES:
        if start <= minutes < end:
            return phase

    if now.hour < START_HOUR:
        return Phase.PRE_WORK
    if now.hour >= END_HOUR:
        return Phase.POST_WORK
    return Phase.WORK


def next_break(now: datetime) -> tuple[Phase, int] | None:
    """The next break today, forward-looking only: (phase, minutes until it
    starts). None once the last break of the day has started.
    """
    minutes = now.hour * 60 + now.minute
    for phase, start, _end in _BREAK_PHASES:
        if minutes < start:
            return phase, start - minutes
    return None


@dataclass(frozen=True, slots=True)
class WorkProgress:
    elapsed: timedelta
    remaining: timedelta
    percentage: int
    message_bucket: int


def resolve_work_progress(now: datetime) -> WorkProgress:
    """Progress through the work day. Only meaningful when `resolve_phase(now)`
    is `Phase.WORK`; callers are expected to check the phase first.
    """
    start_time = now.replace(hour=START_HOUR, minute=0, second=0, microsecond=0)
    end_time = now.replace(hour=END_HOUR, minute=0, second=0, microsecond=0)
    elapsed = now - start_time
    remaining = end_time - now
    total_seconds = (end_time - start_time).total_seconds()
    percentage = max(0, min(100, int(elapsed.total_seconds() / total_seconds * 100)))
    message_bucket = min(percentage // 10, 9)
    return WorkProgress(
        elapsed=elapsed,
        remaining=remaining,
        percentage=percentage,
        message_bucket=message_bucket,
    )


@dataclass(frozen=True, slots=True)
class BreakProgress:
    remaining: timedelta


def resolve_break_progress(now: datetime) -> BreakProgress:
    """Time left in the current break. Only meaningful when `resolve_phase(now)`
    is one of the break phases; callers are expected to check the phase first.
    """
    minutes = now.hour * 60 + now.minute
    for _phase, start, end in _BREAK_PHASES:
        if start <= minutes < end:
            return BreakProgress(remaining=timedelta(minutes=end - minutes))
    return BreakProgress(remaining=timedelta(0))
