"""Resolves the active segment and progress through a built day.

Pure: `(now, plan) -> Resolved`. No clock, no I/O, no rich. `now` is the
clock's aware UTC instant; `plan.segments` are aware local datetimes (see
`domain/schedule.py`'s module docstring) -- comparing or subtracting one
from the other is an ordinary aware-to-aware operation, correct regardless
of either side's UTC offset.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from timeexisting.domain.schedule import DayPlan
from timeexisting.domain.segments import Segment


@dataclass(frozen=True, slots=True)
class Resolved:
    segment: Segment
    elapsed: timedelta
    remaining: timedelta
    next_segment: Segment | None
    expected_credit: timedelta
    progress: float
    past_nominal_end: bool


def _segment_index(now: datetime, plan: DayPlan) -> int:
    for index, segment in enumerate(plan.segments):
        if segment.start <= now < segment.end:
            return index
    if now < plan.segments[0].start:
        return 0
    return len(plan.segments) - 1


def _paid_elapsed(now: datetime, segment: Segment) -> timedelta:
    if not segment.paid:
        return timedelta(0)
    span_end = min(now, segment.end)
    if span_end <= segment.start:
        return timedelta(0)
    return span_end - segment.start


def resolve(now: datetime, plan: DayPlan) -> Resolved:
    index = _segment_index(now, plan)
    segment = plan.segments[index]
    next_segment = plan.segments[index + 1] if index + 1 < len(plan.segments) else None

    elapsed = max(timedelta(0), min(now, segment.end) - segment.start)
    remaining = max(timedelta(0), segment.end - max(now, segment.start))

    expected_credit = sum((_paid_elapsed(now, s) for s in plan.segments), timedelta(0))

    progress = 0.0
    if plan.target > timedelta(0):
        progress = max(0.0, min(1.0, expected_credit / plan.target))

    return Resolved(
        segment=segment,
        elapsed=elapsed,
        remaining=remaining,
        next_segment=next_segment,
        expected_credit=expected_credit,
        progress=progress,
        past_nominal_end=now >= plan.nominal_end,
    )
