"""Event log reconciliation in the collector (roadmap section 8): the System and Winlogon/Operational logs read every tick behind a cursor each, pending sleep gaps resolved against the System log or written from the clock, and the coverage of a gap between runs.

The System log is the primary source of sleep boundaries: under Modern Standby no power broadcast arrives on the work laptop, and the collector keeps ticking through the screen-off phase, so a sleep can begin before the last tick and some never show up as a clock jump. So both channels are read on every tick, not only when a gap is detected.

The cursors and the caller contract of `eventlog.boundaries()`:

- Every System read starts at the System cursor, or at the start of a pending gap when that is earlier, so a retry always re-reads from the gap start.
- The System cursor only moves to the last `suspend` or `boot` boundary returned, never to a `resume` and never past the last boundary returned. A read starting on a hibernate wake would miss the Kernel-Power 42 before it and read the 506 and 507 Windows writes on the way out as a sleep; a read starting on a suspend or a boot sees the whole sleep that follows.
- The Winlogon cursor moves to the last record returned.
- A cursor moves only after the append, so a crash can repeat a record but never lose one.

Input evidence from the wake reason (roadmap section 6, phase 3 step 7). A terminal Kernel-Power 507 whose reason names a user input (`eventlog.INPUT_REASONS`) and that ends a sleep on an unlocked session is written as `input`, source `eventlog`, `observed`, at the 507's own time, with the 507's `detail`. The session at that time comes from the `Evidence` tracker, asked after both channels are read in the same tick, so a lock Winlogon recorded during the standby counts. A lock at the same time as the 507 means locked. Because it comes from the log, it also works for a 507 backfilled into a gap between runs.

Re-reading from the cursor returns records already written. The ledger records transitions only, so nothing is appended twice: a record whose key (event, time, `detail`, which is what replay deduplicates on) is already in the ledger at or after the cursor is skipped. The key set is built at start from this host's records in every shard, and keys before a cursor are dropped as it advances, so the set stays bounded by what one read can return.
"""

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from timeexisting.collector.evidence import Evidence
from timeexisting.ledger.events import Confidence, Event, EventType, Source
from timeexisting.ledger.replay import Coverage
from timeexisting.presence.backend import ChannelCheck, EventLogReader, SessionState
from timeexisting.presence.eventlog import INPUT_REASONS, SYSTEM_CHANNEL, WINLOGON_CHANNEL, Boundary

logger = logging.getLogger(__name__)

# How long a pending sleep gap waits for its System log record before it is written from the clock (section 8).
PENDING_WINDOW = timedelta(minutes=2)

# The System log's own records, and the `input` written from a 507 beside them: their keys share the System cursor.
_SYSTEM_EVENTS = frozenset({EventType.SUSPEND, EventType.RESUME, EventType.BOOT, EventType.INPUT})
_SESSION_EVENTS = frozenset({EventType.LOCK, EventType.UNLOCK})
# The boundaries the System cursor may stand on: the start of a sleep, or a boot.
_CURSOR_EVENTS = frozenset({EventType.SUSPEND, EventType.BOOT})

type Key = tuple[EventType, datetime, str]


class Append(Protocol):
    def __call__(
        self,
        ts: datetime,
        event: EventType,
        source: Source,
        confidence: Confidence,
        data: Mapping[str, str] | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PendingGap:
    """A clock jump waiting for the System log: `start` is the last tick before it, `wake` the first tick after."""

    start: datetime
    wake: datetime


@dataclass(slots=True)
class ReadState:
    """What the checkpoint holds for reconciliation: the two cursors and the pending gaps. A cursor is `None` only when no run has set it yet."""

    system_cursor: datetime | None = None
    winlogon_cursor: datetime | None = None
    pending: list[PendingGap] = field(default_factory=list)

    def record(self) -> dict[str, object]:
        """The checkpoint fields, as aware UTC ISO 8601 strings."""
        record: dict[str, object] = {
            "pending": [
                {"start": gap.start.isoformat(), "wake": gap.wake.isoformat()} for gap in self.pending
            ]
        }
        if self.system_cursor is not None:
            record["system_cursor"] = self.system_cursor.isoformat()
        if self.winlogon_cursor is not None:
            record["winlogon_cursor"] = self.winlogon_cursor.isoformat()
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> ReadState:
        """The state a checkpoint record holds. A checkpoint written before phase 3 step 6 has none of the fields and gives an empty state; a field that does not parse is dropped and logged, never guessed."""
        pending: list[PendingGap] = []
        raw_pending = record.get("pending", [])
        for raw in raw_pending if isinstance(raw_pending, list) else []:
            start = _stamp(raw.get("start") if isinstance(raw, dict) else None)
            wake = _stamp(raw.get("wake") if isinstance(raw, dict) else None)
            if start is None or wake is None or wake < start:
                logger.warning("dropping an unreadable pending gap from the checkpoint: %r", raw)
                continue
            pending.append(PendingGap(start, wake))
        return cls(_stamp(record.get("system_cursor")), _stamp(record.get("winlogon_cursor")), pending)


def _stamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        return None
    return stamp


def _key(event: Event) -> Key | None:
    detail = event.data.get("detail")
    if event.source is not Source.EVENTLOG or detail is None:
        return None
    return (event.event, event.ts, detail)


def _boundary_data(boundary: Boundary) -> dict[str, str]:
    data = {"detail": boundary.detail}
    if boundary.boot_kind is not None:
        data["boot_kind"] = str(boundary.boot_kind)
    return data


def coverage(
    reader: EventLogReader | None, channels: ChannelCheck | None, span_start: datetime, span_end: datetime
) -> Coverage:
    """What the event logs can say about the gap from `span_start` to `span_end`: whether each channel was readable at the self-check, and whether its oldest record is no later than `span_start`. An unreadable channel never reaches back."""

    def reaches_back(readable: bool, channel: str) -> bool:
        if not readable or reader is None:
            return False
        oldest = reader.oldest(channel)
        return oldest is not None and oldest <= span_start

    system = channels is not None and channels.system
    winlogon = channels is not None and channels.winlogon
    return Coverage(
        span_start=span_start,
        span_end=span_end,
        system_readable=system,
        system_reaches_back=reaches_back(system, SYSTEM_CHANNEL),
        winlogon_readable=winlogon,
        winlogon_reaches_back=reaches_back(winlogon, WINLOGON_CHANNEL),
    )


class Reconciler:
    """Reads the event logs for one run and appends what they record. `reader` is `None`, or `channels` says a channel is unreadable, when there is nothing to read: then a clock jump is written from the clock at once, and a WTS-only run reads no Winlogon records.

    `state` is the checkpoint's; a cursor still `None` starts at `read_from`. `ledger` is every event already in the ledger, from every shard: this host's `eventlog` records at or after each cursor seed the key set. `append` writes one event to this host's shard. `evidence`, when given, hears every event this reconciler appends and answers the session at a 507; without it no input is written.
    """

    def __init__(
        self,
        reader: EventLogReader | None,
        channels: ChannelCheck | None,
        state: ReadState,
        read_from: datetime,
        host: str,
        ledger: Iterable[Event],
        append: Append,
        evidence: Evidence | None = None,
    ) -> None:
        self.state = state
        self._evidence = evidence
        self._reader = reader
        self._system = reader is not None and channels is not None and channels.system
        self._winlogon = reader is not None and channels is not None and channels.winlogon
        self._write = append
        if state.system_cursor is None:
            state.system_cursor = read_from
        if state.winlogon_cursor is None:
            state.winlogon_cursor = read_from
        self._system_keys: set[Key] = set()
        self._session_keys: set[Key] = set()
        for event in ledger:
            key = _key(event)
            if key is None or event.host != host:
                continue
            if event.event in _SYSTEM_EVENTS and event.ts >= state.system_cursor:
                self._system_keys.add(key)
            elif event.event in _SESSION_EVENTS and event.ts >= state.winlogon_cursor:
                self._session_keys.add(key)

    def _append(
        self,
        ts: datetime,
        event: EventType,
        source: Source,
        confidence: Confidence,
        data: Mapping[str, str] | None = None,
    ) -> None:
        self._write(ts, event, source, confidence, data)
        if self._evidence is not None:
            self._evidence.heard(ts, event, source, (data or {}).get("boot_kind"))

    def jump(self, start: datetime, wake: datetime) -> None:
        """A clock jump from the tick at `start` to the tick at `wake`. Held as pending for the System log when it is readable; written from the clock at once when it is not."""
        gap = PendingGap(start, wake)
        if self._system:
            self.state.pending.append(gap)
        else:
            self._write_clock(gap)

    def tick(self, now: datetime) -> None:
        """One reconciliation pass at `now`: the System log, then Winlogon/Operational, then input from the 507s read, then the pending gaps."""
        cursor = self.state.system_cursor
        found = self._read_system(now) if self._system else []
        if self._winlogon:
            self._read_winlogon()
        if cursor is not None:
            self._input(found, cursor)
        self._settle(now, found)

    def flush(self) -> None:
        """Write every pending gap from the clock. For a clean stop, which deletes the checkpoint the gaps live in."""
        for gap in self.state.pending:
            self._write_clock(gap)
        self.state.pending = []

    def _read_system(self, now: datetime) -> list[Boundary]:
        assert self._reader is not None and self.state.system_cursor is not None
        cursor = self.state.system_cursor
        since = min([cursor, *(gap.start for gap in self.state.pending)])
        found = self._reader.system_boundaries(since, now)
        for boundary in found:
            key = (boundary.event, boundary.time, boundary.detail)
            if boundary.time < cursor or key in self._system_keys:
                continue
            self._append(
                boundary.time, boundary.event, Source.EVENTLOG, Confidence.OBSERVED, _boundary_data(boundary)
            )
            self._system_keys.add(key)
        stops = [boundary.time for boundary in found if boundary.event in _CURSOR_EVENTS]
        if stops and max(stops) > cursor:
            self.state.system_cursor = max(stops)
            self._system_keys = {key for key in self._system_keys if key[1] >= self.state.system_cursor}
        return found

    def _read_winlogon(self) -> None:
        assert self._reader is not None and self.state.winlogon_cursor is not None
        cursor = self.state.winlogon_cursor
        records = self._reader.session_records(cursor)
        for record in records:
            key = (record.event, record.time, record.detail)
            if record.time < cursor or key in self._session_keys:
                continue
            self._append(
                record.time, record.event, Source.EVENTLOG, Confidence.OBSERVED, {"detail": record.detail}
            )
            self._session_keys.add(key)
        latest = max((record.time for record in records), default=cursor)
        if latest > cursor:
            self.state.winlogon_cursor = latest
            self._session_keys = {key for key in self._session_keys if key[1] >= latest}

    def _input(self, found: list[Boundary], cursor: datetime) -> None:
        """`input` for each wake in `found`, at or after the cursor the read started from, whose 507 reason names a user input and whose session was unlocked. Keyed like the boundaries, so a re-read writes nothing twice, and a crash between a wake and its input writes the input on the next read."""
        if self._evidence is None:
            return
        for boundary in found:
            if boundary.event is not EventType.RESUME or boundary.reason not in INPUT_REASONS:
                continue
            key = (EventType.INPUT, boundary.time, boundary.detail)
            if boundary.time < cursor or key in self._system_keys:
                continue
            if self._evidence.session_at(boundary.time) is not SessionState.UNLOCKED:
                continue
            logger.info("input from the wake at %s: %s", boundary.time.isoformat(), boundary.detail)
            self._append(
                boundary.time,
                EventType.INPUT,
                Source.EVENTLOG,
                Confidence.OBSERVED,
                {"detail": boundary.detail},
            )
            self._system_keys.add(key)

    def _settle(self, now: datetime, found: list[Boundary]) -> None:
        waiting: list[PendingGap] = []
        for gap in self.state.pending:
            if any(b.event is EventType.RESUME and gap.start <= b.time <= gap.wake for b in found):
                logger.info("sleep from %s resolved against the System log", gap.start.isoformat())
            elif now - gap.wake >= PENDING_WINDOW:
                logger.info(
                    "no System record for the sleep from %s; writing it from the clock", gap.start.isoformat()
                )
                self._write_clock(gap)
            else:
                waiting.append(gap)
        self.state.pending = waiting

    def _write_clock(self, gap: PendingGap) -> None:
        self._append(gap.start, EventType.SUSPEND, Source.CLOCK, Confidence.INFERRED)
        self._append(gap.wake, EventType.RESUME, Source.CLOCK, Confidence.INFERRED)
