"""What remains of the working week, planned from the schedule.

Pure: `(resolved, cfg, excluded) -> timedelta`. No clock, no I/O, no rich. The figure is planned, not credited: today's remaining scheduled working time plus the daily target for each working day left in the ISO week. Phase 4 replaces it with the weekly target minus credited time.
"""

from collections.abc import Callable
from datetime import date, timedelta

from timeexisting.config.models import Config
from timeexisting.domain.resolver import Resolved

# Index-aligned with `date.weekday()`: Monday = 0 .. Sunday = 6. The spelling of `contract.working_days`.
WEEKDAY_ABBREVIATIONS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def working_weekdays(cfg: Config) -> frozenset[int]:
    return frozenset(WEEKDAY_ABBREVIATIONS.index(day) for day in cfg.contract.working_days)


def _never(_day: date) -> bool:
    return False


def remaining_week(
    resolved: Resolved, cfg: Config, *, excluded: Callable[[date], bool] = _never
) -> timedelta:
    """Today's remaining window plus `daily_target` times the working days after today in the ISO week.

    Today comes from its built plan, so today's flags already apply: a sick day contributes nothing, a half day half. A later day counts when it is one of `contract.working_days` and `excluded` does not reject it; `excluded` is where holidays and flagged later days are ruled out. Nothing supplies those yet: the calendar and day-flag events arrive in phase 4.
    """
    today = resolved.plan.day
    today_left = max(timedelta(0), resolved.plan.target - resolved.expected_credit)
    working = working_weekdays(cfg)
    later_days = (today + timedelta(days=offset) for offset in range(1, 7 - today.weekday()))
    counted = sum(1 for day in later_days if day.weekday() in working and not excluded(day))
    return today_left + counted * cfg.contract.daily_target
