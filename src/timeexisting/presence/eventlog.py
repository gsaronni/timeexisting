"""Event log records to typed boundaries (roadmap sections 6 and 8): the pure half of the event log reader.

Pure. No clock, no I/O, no platform imports: the caller hands in each record's XML as `EvtRender` produced it, and the instant up to which the log has been read. `presence/eventlog_win32.py` is the only code that touches the Windows API; `presence/backend.py` joins the two.

Only the records phase 3 step 0 confirmed are parsed. System: Kernel-Power 506 and 507 (Modern Standby entry and exit) and 42 (hibernation entry), Power-Troubleshooter 1 (a hibernate wake), Kernel-General 1 (a clock change) and 12 and 13 (OS start and stop), EventLog 6005 and 6006 (event log service start and stop). Winlogon/Operational: 811 from the `Sens` subscriber. Kernel-Power 107 is never parsed, since its timestamp is the clock frozen at hibernation entry, and 566 is never parsed, since its reason names do not mean what they say.

`SystemTime` and every time field in these records is UTC, written with seven fractional digits. `datetime` holds six, so the 100 ns digit is dropped (truncated, never rounded): two records less than a microsecond apart compare equal.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from xml.etree import ElementTree

from timeexisting.ledger.events import EventType

SYSTEM_CHANNEL = "System"
WINLOGON_CHANNEL = "Microsoft-Windows-Winlogon/Operational"

# A 507 followed by a 506 within this is a chain step, not a wake (section 6).
CHAIN_STEP = timedelta(seconds=2)
# A Kernel-General 1 Reason 2 correction forward by more than this, after a Kernel-Power 42, is the hibernate wake. The clock-jump threshold (section 3); the largest correction outside a hibernate wake at step 0 was 2.6 s.
HIBERNATE_JUMP = timedelta(seconds=60)
# How long a Kernel-General 1 wake waits for the Power-Troubleshooter 1 that outranks it. At a hibernate wake the Power-Troubleshooter 1 is written about 4.5 s after the clock change (0.5 to 2.5 s after its own WakeTime at step 0); this leaves room, and the pending gap retries for two minutes anyway (section 8).
WAKE_REPORT_LAG = timedelta(seconds=30)

_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"

_KERNEL_POWER = "Microsoft-Windows-Kernel-Power"
_POWER_TROUBLESHOOTER = "Microsoft-Windows-Power-Troubleshooter"
_KERNEL_GENERAL = "Microsoft-Windows-Kernel-General"
_EVENTLOG = "EventLog"
_WINLOGON = "Microsoft-Windows-Winlogon"

_SYSTEM_IDS = {
    _KERNEL_POWER: (506, 507, 42),
    _POWER_TROUBLESHOOTER: (1,),
    _KERNEL_GENERAL: (1, 12, 13),
    _EVENTLOG: (6005, 6006),
}
_WINLOGON_ID = 811
_SENS = "Sens"
# Winlogon/Operational 811 `Sens` codes (section 5). Inferred from the record shape at step 0, not rendered by Windows; confirmed by the step 5 live check before the collector reads the channel. Every other code (12, shell start, among them) is not a lock or an unlock.
_SENS_CODES = {
    4: EventType.LOCK,
    3: EventType.LOCK,
    5: EventType.UNLOCK,
    2: EventType.UNLOCK,
}
# Kernel-General 1 `Reason` 2: the clock set from the RTC, as at a hibernate wake.
_CLOCK_FROM_RTC = 2


class EventLogFormatError(ValueError):
    """An event record whose XML does not match the schema it claims: unparseable, or a field missing or malformed."""


class EventLogUnavailable(Exception):
    """A channel that cannot be queried: unreadable, absent, or the query itself refused. Raised by the I/O layer, never by the parser."""


class BootKind(StrEnum):
    START = "start"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class PowerTransition:
    """Kernel-Power 506 (entering Modern Standby) or 507 (exiting it), with its `Reason`."""

    time: datetime
    event_id: int
    reason: int


@dataclass(frozen=True, slots=True)
class HibernateEntry:
    """Kernel-Power 42: entering sleep, which on a Modern Standby machine is hibernation."""

    time: datetime
    reason: int
    target_state: int


@dataclass(frozen=True, slots=True)
class WakeReport:
    """Power-Troubleshooter 1: the machine returned from a low-power state. `wake_time` is the real wake."""

    time: datetime
    sleep_time: datetime
    wake_time: datetime


@dataclass(frozen=True, slots=True)
class ClockChange:
    """Kernel-General 1: the system time changed from `old_time` to `new_time`."""

    time: datetime
    old_time: datetime
    new_time: datetime
    reason: int


@dataclass(frozen=True, slots=True)
class BootRecord:
    """EventLog 6005 or Kernel-General 12 (a start), EventLog 6006 or Kernel-General 13 (a shutdown). `time` is the record's own `TimeCreated`."""

    time: datetime
    kind: BootKind
    provider: str
    event_id: int


type SystemRecord = PowerTransition | HibernateEntry | WakeReport | ClockChange | BootRecord


@dataclass(frozen=True, slots=True)
class Boundary:
    """A machine boundary the System log records: `suspend`, `resume` or `boot`, at its logged time. `detail` names the record and, for 506, 507 and 42, its reason, and is what the ledger's `detail` carries. `reason` is the numeric 507 reason on a resume from a 507, for the input evidence rule (section 6), and `None` otherwise. `boot_kind` is `start` or `stop` on a `boot`, from its record, and `None` otherwise: no caller reads it from `detail`."""

    time: datetime
    event: EventType
    detail: str
    reason: int | None = None
    boot_kind: BootKind | None = None


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """Winlogon/Operational 811 from `Sens` for this user, read as `lock` or `unlock`, with the Winlogon code."""

    time: datetime
    event: EventType
    code: int

    @property
    def detail(self) -> str:
        """The channel and the code, which is what replay deduplicates on (section 5)."""
        return f"Winlogon/Operational {_WINLOGON_ID} {_SENS} {self.code}"


def _xpath_time(since: datetime) -> str:
    if since.tzinfo is None or since.utcoffset() is None:
        raise ValueError(f"since must be timezone-aware: {since.isoformat()}")
    utc = since.astimezone(UTC)
    # Truncated to the millisecond, never rounded up, so the filter can only reach further back.
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


def _since(since: datetime | None) -> str:
    return "" if since is None else f" and TimeCreated[@SystemTime>='{_xpath_time(since)}']"


def system_xpath(since: datetime | None) -> str:
    """The System log query: the confirmed providers and event IDs, `TimeCreated` at or after `since`, or every such record when `since` is `None`. The provider and event ID filter is always in the query, never applied after a broad read."""
    providers = " or ".join(
        f"(Provider[@Name='{provider}'] and ({' or '.join(f'EventID={event_id}' for event_id in ids)}))"
        for provider, ids in _SYSTEM_IDS.items()
    )
    return f"*[System[({providers}){_since(since)}]]"


def winlogon_xpath(since: datetime | None) -> str:
    """The Winlogon/Operational query: 811, `TimeCreated` at or after `since`, or every 811 when `since` is `None`. The subscriber, the code and the user are checked by `parse_winlogon`."""
    return f"*[System[Provider[@Name='{_WINLOGON}'] and EventID={_WINLOGON_ID}{_since(since)}]]"


@dataclass(frozen=True, slots=True)
class _Parsed:
    provider: str
    event_id: int
    time: datetime
    user_id: str | None
    data: dict[str, str]


def _parse_time(text: str | None, field: str) -> datetime:
    if not text:
        raise EventLogFormatError(f"{field} is missing")
    try:
        value = datetime.fromisoformat(text)
    except ValueError as error:
        raise EventLogFormatError(f"{field} is not ISO 8601: {text!r}") from error
    if value.tzinfo is None or value.utcoffset() is None:
        raise EventLogFormatError(f"{field} is not UTC: {text!r}")
    return value.astimezone(UTC)


def _parse_int(text: str | None, field: str) -> int:
    try:
        return int(text or "")
    except ValueError as error:
        raise EventLogFormatError(f"{field} is not an integer: {text!r}") from error


def _parse(xml: str) -> _Parsed:
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as error:
        raise EventLogFormatError(f"not well-formed XML: {error}") from error
    system = root.find(f"{_NS}System")
    if system is None:
        raise EventLogFormatError("no System element")
    provider = system.find(f"{_NS}Provider")
    if provider is None or not provider.get("Name"):
        raise EventLogFormatError("no Provider Name")
    event_id = system.find(f"{_NS}EventID")
    created = system.find(f"{_NS}TimeCreated")
    security = system.find(f"{_NS}Security")
    data = {
        element.get("Name", ""): element.text or ""
        for element in root.iterfind(f"{_NS}EventData/{_NS}Data")
        if element.get("Name")
    }
    return _Parsed(
        provider=provider.get("Name", ""),
        event_id=_parse_int(event_id.text if event_id is not None else None, "EventID"),
        time=_parse_time(created.get("SystemTime") if created is not None else None, "TimeCreated"),
        user_id=security.get("UserID") if security is not None else None,
        data=data,
    )


def record_time(xml: str) -> datetime:
    """Any record's `TimeCreated`, aware UTC. Raises `EventLogFormatError` when it is missing or malformed."""
    return _parse(xml).time


def parse_system(xml: str) -> SystemRecord | None:
    """One System log record as a typed record, or `None` when it is not one of the confirmed records (a Kernel-Power 107 or 566 among them). Raises `EventLogFormatError` for a confirmed record that is malformed."""
    record = _parse(xml)
    key = (record.provider, record.event_id)
    data = record.data
    if key in {(_KERNEL_POWER, 506), (_KERNEL_POWER, 507)}:
        return PowerTransition(record.time, record.event_id, _parse_int(data.get("Reason"), "Reason"))
    if key == (_KERNEL_POWER, 42):
        return HibernateEntry(
            record.time,
            _parse_int(data.get("Reason"), "Reason"),
            _parse_int(data.get("TargetState"), "TargetState"),
        )
    if key == (_POWER_TROUBLESHOOTER, 1):
        return WakeReport(
            record.time,
            _parse_time(data.get("SleepTime"), "SleepTime"),
            _parse_time(data.get("WakeTime"), "WakeTime"),
        )
    if key == (_KERNEL_GENERAL, 1):
        return ClockChange(
            record.time,
            _parse_time(data.get("OldTime"), "OldTime"),
            _parse_time(data.get("NewTime"), "NewTime"),
            _parse_int(data.get("Reason"), "Reason"),
        )
    if key in {(_KERNEL_GENERAL, 12), (_EVENTLOG, 6005)}:
        return BootRecord(record.time, BootKind.START, record.provider, record.event_id)
    if key in {(_KERNEL_GENERAL, 13), (_EVENTLOG, 6006)}:
        return BootRecord(record.time, BootKind.STOP, record.provider, record.event_id)
    return None


def parse_winlogon(xml: str, user_sid: str) -> SessionRecord | None:
    """One Winlogon/Operational record as `lock` or `unlock`, or `None` when it is not an 811 from `Sens` with code 2, 3, 4 or 5 for `user_sid`. Raises `EventLogFormatError` for an 811 that is malformed."""
    record = _parse(xml)
    if record.provider != _WINLOGON or record.event_id != _WINLOGON_ID:
        return None
    if record.data.get("SubscriberName") != _SENS:
        return None
    if record.user_id is None or record.user_id.upper() != user_sid.upper():
        return None
    code = _parse_int(record.data.get("Event"), "Event")
    event = _SENS_CODES.get(code)
    if event is None:
        return None
    return SessionRecord(record.time, event, code)


def _short(provider: str) -> str:
    return provider.removeprefix("Microsoft-Windows-")


def _next_506(records: list[SystemRecord], index: int) -> PowerTransition | None:
    for record in records[index + 1 :]:
        if isinstance(record, PowerTransition) and record.event_id == 506:
            return record
    return None


class _Unsettled(Exception):
    """The log cannot settle a record yet; it and everything after it wait for the next read."""


def _hibernate_wake(
    records: list[SystemRecord], index: int, until: datetime
) -> tuple[Boundary, datetime] | None:
    """The wake ending the hibernation that the 42 at `index` entered, and the instant up to which the 506 and 507 records written on the way out belong to it. `None` when the log reports no wake before the next 42 or boot record. Raises `_Unsettled` for a Kernel-General 1 wake younger than `WAKE_REPORT_LAG`."""
    window: list[SystemRecord] = []
    for record in records[index + 1 :]:
        if isinstance(record, HibernateEntry | BootRecord):
            break
        window.append(record)
    report = next((record for record in window if isinstance(record, WakeReport)), None)
    if report is not None:
        return Boundary(
            report.wake_time, EventType.RESUME, "System Power-Troubleshooter 1 WakeTime"
        ), report.time
    jump = next(
        (
            record
            for record in window
            if isinstance(record, ClockChange)
            and record.reason == _CLOCK_FROM_RTC
            and record.new_time - record.old_time > HIBERNATE_JUMP
        ),
        None,
    )
    if jump is None:
        return None
    if until - jump.new_time < WAKE_REPORT_LAG:
        raise _Unsettled
    wake = Boundary(jump.new_time, EventType.RESUME, "System Kernel-General 1 reason 2 NewTime")
    return wake, jump.new_time + WAKE_REPORT_LAG


def boundaries(records: Iterable[SystemRecord], *, until: datetime) -> list[Boundary]:
    """The machine boundaries the records give, in time order. `until` is the instant up to which the log has been read.

    - Sleep: `suspend` at the first 506 of a chain, `resume` at the terminal 507. A 507 followed by a 506 within `CHAIN_STEP` is a chain step and gives nothing.
    - Hibernation: `suspend` at the 42, unless the machine is already asleep. The wake is the Power-Troubleshooter 1 `WakeTime`, or, when none follows before the next 42 or boot record, the `NewTime` of a Kernel-General 1 Reason 2 that moved the clock forward by more than `HIBERNATE_JUMP`. Windows passes through Modern Standby on the way out of hibernation, writing a 506 and a 507 between the clock change and the Power-Troubleshooter 1; those belong to the wake and give nothing. When the log reports neither wake record, a 507 ends the hibernation as it ends any sleep.
    - Boot: `boot` at each record's own time. A boot record ends any sleep in progress.

    A record the log cannot settle yet ends the result there, and every record from it on is left for the next read: a 507 younger than `CHAIN_STEP` with no 506 after it, since that 506 may still come, and a Kernel-General 1 wake younger than `WAKE_REPORT_LAG`, since a Power-Troubleshooter 1 may still come. Power-Troubleshooter 1 and Kernel-General 1 records give a boundary only through the hibernation they end.

    The caller's contract follows from that. A retry re-reads from the caller's own start, not from the last boundary returned, and relies on the ledger's deduplication of repeated `eventlog` records to absorb what it appends twice. A cursor never advances past the last boundary returned, since records after it may not have been settled.
    """
    ordered = sorted(records, key=lambda record: record.time)
    result: list[Boundary] = []
    asleep = False
    # 506 and 507 records up to this instant were written on the way out of a hibernation already ended.
    absorbed_until: datetime | None = None
    for index, record in enumerate(ordered):
        match record:
            case PowerTransition() if absorbed_until is not None and record.time <= absorbed_until:
                continue
            case PowerTransition(event_id=506):
                if not asleep:
                    result.append(
                        Boundary(
                            record.time, EventType.SUSPEND, f"System Kernel-Power 506 reason {record.reason}"
                        )
                    )
                    asleep = True
            case PowerTransition(event_id=507):
                following = _next_506(ordered, index)
                if following is not None and following.time - record.time <= CHAIN_STEP:
                    continue
                if following is None and until - record.time < CHAIN_STEP:
                    break
                result.append(
                    Boundary(
                        record.time,
                        EventType.RESUME,
                        f"System Kernel-Power 507 reason {record.reason}",
                        record.reason,
                    )
                )
                asleep = False
            case HibernateEntry():
                if not asleep:
                    result.append(
                        Boundary(
                            record.time, EventType.SUSPEND, f"System Kernel-Power 42 reason {record.reason}"
                        )
                    )
                    asleep = True
                try:
                    wake = _hibernate_wake(ordered, index, until)
                except _Unsettled:
                    break
                if wake is not None:
                    boundary, absorbed_until = wake
                    result.append(boundary)
                    asleep = False
            case BootRecord():
                result.append(
                    Boundary(
                        record.time,
                        EventType.BOOT,
                        f"System {_short(record.provider)} {record.event_id}",
                        boot_kind=record.kind,
                    )
                )
                asleep = False
    return sorted(result, key=lambda boundary: boundary.time)
