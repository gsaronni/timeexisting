"""The ledger's event schema (roadmap section 5): one frozen `Event` per line, serialised to one JSON object with keys in a fixed order.

Pure. No clock, no I/O: `Event.new` takes `ts` as a parameter and the only thing it generates is the id, which is `uuid.uuid7()` and therefore time-ordered without this module ever reading the time itself.
"""

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

from timeexisting.config.models import Config

SCHEMA_VERSION = 1

_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_REQUIRED_KEYS = ("v", "id", "ts", "host", "profile", "event", "source", "confidence")
_OPTIONAL_KEYS = ("data",)
_KEY_ORDER = _REQUIRED_KEYS + _OPTIONAL_KEYS
_EMPTY: Mapping[str, str] = MappingProxyType({})


class LedgerFormatError(ValueError):
    """A ledger line, or the fields of an event, that do not match the schema."""


class EventType(StrEnum):
    COLLECTOR_START = "collector_start"
    COLLECTOR_STOP = "collector_stop"
    LOCK = "lock"
    UNLOCK = "unlock"
    SUSPEND = "suspend"
    RESUME = "resume"
    CLASSIFY = "classify"
    NOTE = "note"
    DAYFLAG = "dayflag"
    MANUAL_START = "manual_start"
    MANUAL_END = "manual_end"
    VOID = "void"


class Source(StrEnum):
    WIN32 = "win32"
    EVENTLOG = "eventlog"
    DBUS = "dbus"
    COLLECTOR = "collector"
    MANUAL = "manual"
    BACKFILL = "backfill"


class Confidence(StrEnum):
    OBSERVED = "observed"
    INFERRED = "inferred"


@dataclass(frozen=True, slots=True)
class Event:
    """One ledger line. `ts` is always aware UTC: an aware timestamp in any other zone is converted, a naive one is rejected. `data` holds event-specific string fields, such as the `reason` on `collector_start` and `collector_stop`."""

    v: int
    id: str
    ts: datetime
    host: str
    profile: str
    event: EventType
    source: Source
    confidence: Confidence
    data: Mapping[str, str] = field(default=_EMPTY, hash=False)

    def __post_init__(self) -> None:
        if type(self.v) is not int or self.v != SCHEMA_VERSION:
            raise LedgerFormatError(f"unknown schema version: {self.v!r}")
        if not isinstance(self.id, str) or _ID_PATTERN.match(self.id) is None:
            raise LedgerFormatError(f"id must be 32 lowercase hex characters: {self.id!r}")
        if not isinstance(self.ts, datetime):
            raise LedgerFormatError(f"ts must be a datetime: {self.ts!r}")
        if self.ts.tzinfo is None or self.ts.utcoffset() is None:
            raise LedgerFormatError(f"ts must be timezone-aware: {self.ts.isoformat()}")
        for name in ("host", "profile"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise LedgerFormatError(f"{name} must be a non-empty string: {value!r}")
        if not isinstance(self.data, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in self.data.items()
        ):
            raise LedgerFormatError(f"data must map strings to strings: {self.data!r}")
        object.__setattr__(self, "ts", self.ts.astimezone(UTC))
        object.__setattr__(self, "event", _member(EventType, self.event, "event"))
        object.__setattr__(self, "source", _member(Source, self.source, "source"))
        object.__setattr__(self, "confidence", _member(Confidence, self.confidence, "confidence"))
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)) if self.data else _EMPTY)

    @classmethod
    def new(
        cls,
        *,
        ts: datetime,
        host: str,
        profile: str,
        event: EventType,
        source: Source,
        confidence: Confidence = Confidence.OBSERVED,
        data: Mapping[str, str] | None = None,
    ) -> Event:
        """A fresh event at `ts`, with the current schema version and a new time-ordered id. The caller supplies the time; this never reads a clock."""
        return cls(
            v=SCHEMA_VERSION,
            id=uuid.uuid7().hex,
            ts=ts,
            host=host,
            profile=profile,
            event=event,
            source=source,
            confidence=confidence,
            data=data or _EMPTY,
        )


def _member[E: StrEnum](enum: type[E], value: object, name: str) -> E:
    try:
        return enum(value)
    except ValueError as error:
        raise LedgerFormatError(f"unknown {name}: {value!r}") from error


def to_json(event: Event) -> str:
    """One line, no trailing newline, keys in schema order. `data` is written only when it holds something."""
    record: dict[str, object] = {
        "v": event.v,
        "id": event.id,
        "ts": event.ts.isoformat(),
        "host": event.host,
        "profile": event.profile,
        "event": str(event.event),
        "source": str(event.source),
        "confidence": str(event.confidence),
    }
    if event.data:
        record["data"] = dict(event.data)
    return json.dumps(record, ensure_ascii=False)


def from_json(line: str) -> Event:
    """Parse one ledger line. Anything that does not match the schema exactly raises `LedgerFormatError`: bad JSON, a missing or unknown key, a naive timestamp, an unknown schema version or enum value."""
    try:
        record = json.loads(line)
    except json.JSONDecodeError as error:
        raise LedgerFormatError(f"not valid JSON: {error}") from error
    if not isinstance(record, dict):
        raise LedgerFormatError(f"not a JSON object: {line!r}")
    if "v" in record and (type(record["v"]) is not int or record["v"] != SCHEMA_VERSION):
        raise LedgerFormatError(f"unknown schema version: {record['v']!r}")
    missing = [key for key in _REQUIRED_KEYS if key not in record]
    if missing:
        raise LedgerFormatError(f"missing keys: {', '.join(missing)}")
    unknown = sorted(set(record) - set(_KEY_ORDER))
    if unknown:
        raise LedgerFormatError(f"unknown keys: {', '.join(unknown)}")
    raw_ts = record["ts"]
    if not isinstance(raw_ts, str):
        raise LedgerFormatError(f"ts must be an ISO 8601 string: {raw_ts!r}")
    try:
        ts = datetime.fromisoformat(raw_ts)
    except ValueError as error:
        raise LedgerFormatError(f"ts is not ISO 8601: {raw_ts!r}") from error
    return Event(
        v=record["v"],
        id=record["id"],
        ts=ts,
        host=record["host"],
        profile=record["profile"],
        event=record["event"],
        source=record["source"],
        confidence=record["confidence"],
        data=record.get("data", _EMPTY),
    )


def profile_for(host: str, cfg: Config) -> str:
    """The profile a host's events are recorded under: its entry in `[profiles.hosts]`, else `[profiles].default`."""
    return cfg.profiles.hosts.get(host, cfg.profiles.default)
