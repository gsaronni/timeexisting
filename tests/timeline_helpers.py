"""Builders shared by the timeline tests: synthetic events, expected intervals and the replay call. Times are written in local Copenhagen time for readability and stored in UTC like every event.

A test module imports these from here, never from another test module. `tests` is a package, so pytest puts the repository root on `sys.path` when it imports a test module, and `tests.timeline_helpers` resolves the same way from any working directory and on every runner.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from timeexisting.ledger.events import Confidence, Event, EventType, Source
from timeexisting.ledger.replay import Coverage, Interval, IntervalType, replay

_CPH = ZoneInfo("Europe/Copenhagen")
_HOST = "laptop"

OBS = Confidence.OBSERVED
INF = Confidence.INFERRED

PRESENT = IntervalType.PRESENT
AWAY = IntervalType.AWAY
ASLEEP = IntervalType.ASLEEP
OFF = IntervalType.OFF
UNEVIDENCED = IntervalType.UNEVIDENCED
DOWNTIME = IntervalType.DOWNTIME


def _t(day: int, hour: int, minute: int, second: int = 0, *, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, second, tzinfo=_CPH).astimezone(UTC)


def _event(
    ts: datetime,
    event: EventType,
    source: Source,
    confidence: Confidence = OBS,
    host: str = _HOST,
    **data: str,
) -> Event:
    return Event.new(
        ts=ts, host=host, profile="work", event=event, source=source, confidence=confidence, data=data
    )


def start(ts: datetime, reason: str = "launch", confidence: Confidence = OBS, host: str = _HOST) -> Event:
    return _event(ts, EventType.COLLECTOR_START, Source.COLLECTOR, confidence, host, reason=reason)


def stop(ts: datetime, reason: str, confidence: Confidence = OBS) -> Event:
    return _event(ts, EventType.COLLECTOR_STOP, Source.COLLECTOR, confidence, reason=reason)


def lock(ts: datetime, source: Source = Source.EVENTLOG, confidence: Confidence = OBS, **data: str) -> Event:
    if source is Source.EVENTLOG and not data:
        data = {"detail": "Winlogon/Operational 811 Sens 4"}
    return _event(ts, EventType.LOCK, source, confidence, **data)


def unlock(
    ts: datetime, source: Source = Source.EVENTLOG, confidence: Confidence = OBS, host: str = _HOST
) -> Event:
    data = {"detail": "Winlogon/Operational 811 Sens 5"} if source is Source.EVENTLOG else {}
    return _event(ts, EventType.UNLOCK, source, confidence, host, **data)


def suspend(ts: datetime, source: Source = Source.EVENTLOG, confidence: Confidence = OBS) -> Event:
    return _event(ts, EventType.SUSPEND, source, confidence)


def resume(ts: datetime, source: Source = Source.EVENTLOG, confidence: Confidence = OBS) -> Event:
    return _event(ts, EventType.RESUME, source, confidence)


def user_input(ts: datetime, source: Source = Source.WIN32) -> Event:
    return _event(ts, EventType.INPUT, source)


def iv(
    kind: IntervalType,
    begin: datetime,
    end: datetime | None,
    source: Source = Source.EVENTLOG,
    confidence: Confidence = OBS,
    host: str = _HOST,
) -> Interval:
    return Interval(kind, begin, end, host, confidence, source)


def boot(ts: datetime, kind: str, source: Source = Source.EVENTLOG, detail: str | None = None) -> Event:
    if detail is None:
        detail = (
            "boot_time"
            if source is Source.COLLECTOR
            else f"System EventLog {6005 if kind == 'start' else 6006}"
        )
    return _event(ts, EventType.BOOT, source, detail=detail, boot_kind=kind)


def covered(span_start: datetime, span_end: datetime, **flags: bool) -> Event:
    """The coverage note the collector writes at the start that ends the gap; every flag true unless given."""
    values = {
        "system_readable": True,
        "system_reaches_back": True,
        "winlogon_readable": True,
        "winlogon_reaches_back": True,
    } | flags
    data = Coverage(span_start=span_start, span_end=span_end, **values).data()
    return _event(span_end, EventType.NOTE, Source.COLLECTOR, **data)


def intervals(*events: Event, micro_max: timedelta = timedelta()) -> tuple[Interval, ...]:
    return replay(events, micro_max=micro_max).intervals


def present_time(timeline: tuple[Interval, ...], begin: datetime, end: datetime) -> timedelta:
    total = timedelta()
    for interval in timeline:
        if interval.kind is PRESENT:
            interval_end = interval.end if interval.end is not None else end
            total += max(timedelta(), min(interval_end, end) - max(interval.start, begin))
    return total
