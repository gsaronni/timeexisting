"""Segment shapes: the vocabulary a built day is expressed in.

`Segment` is the atomic unit `domain/schedule.py` produces: a half-open
`[start, end)` span of a single phase within one calendar day. `paid`
distinguishes segments that count toward the daily target (working time)
from ones that don't (lunch, pre/post-work, off day, off hours).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Phase(StrEnum):
    PRE_WORK = "pre_work"
    WORKING = "working"
    LUNCH = "lunch"
    POST_WORK = "post_work"
    OFF_DAY = "off_day"
    OFF_HOURS = "off_hours"


@dataclass(frozen=True, slots=True)
class Segment:
    start: datetime
    end: datetime
    phase: Phase
    label: str
    paid: bool
