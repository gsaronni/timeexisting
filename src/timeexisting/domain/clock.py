"""Clock protocol and implementations.

domain/ never calls a clock directly; every function that needs the current
time takes `now` as a parameter, supplied by one of these.
"""

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at


class ScaledClock:
    """Runs simulated time from `start`, advancing `factor` seconds of
    simulated time per real second elapsed since construction.
    """

    def __init__(self, start: datetime, factor: float, real_clock: Clock | None = None) -> None:
        self._start = start
        self._factor = factor
        self._real_clock = real_clock if real_clock is not None else SystemClock()
        self._real_start = self._real_clock.now()

    def now(self) -> datetime:
        real_elapsed = (self._real_clock.now() - self._real_start).total_seconds()
        return self._start + timedelta(seconds=real_elapsed * self._factor)
