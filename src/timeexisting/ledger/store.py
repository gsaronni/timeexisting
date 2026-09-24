"""The append-only ledger on disk: one JSONL shard per host under `paths.ledger_dir()` (roadmap section 5).

Writes are one event per call: open with `O_APPEND`, write one UTF-8 encoded line, fsync, close. Nothing is buffered across calls, so an event is on disk before `append` returns. Reads never fail on content: a malformed line is skipped, counted and logged, and the rest of the shard still loads.
"""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from timeexisting import paths
from timeexisting.ledger.events import Event, LedgerFormatError, from_json, to_json

logger = logging.getLogger(__name__)

SHARD_SUFFIX = ".jsonl"
ENCODING = "utf-8"

# Windows opens descriptors in text mode unless told otherwise, which would turn every "\n" into "\r\n". Read access too, so `append` can check the last byte before writing.
_WRITE_FLAGS = os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)


@dataclass(frozen=True, slots=True)
class ReadResult:
    events: tuple[Event, ...]
    malformed: int


def shard_path(host: str) -> Path:
    return paths.ledger_dir() / f"{paths.sanitise_host(host)}{SHARD_SUFFIX}"


def _ends_torn(descriptor: int) -> bool:
    """True when the file is non-empty and its last byte is not a newline: a line cut short by a crash."""
    if os.fstat(descriptor).st_size == 0:
        return False
    os.lseek(descriptor, -1, os.SEEK_END)
    return os.read(descriptor, 1) != b"\n"


def append(event: Event) -> None:
    """Append one event to its host's shard and fsync before returning. If the shard ends in a torn line, a newline goes in front of the event in the same write, so the torn line stays one malformed line instead of swallowing this event."""
    payload = (to_json(event) + "\n").encode(ENCODING)
    descriptor = os.open(shard_path(event.host), _WRITE_FLAGS, 0o644)
    try:
        if _ends_torn(descriptor):
            payload = b"\n" + payload
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_shard(path: Path) -> ReadResult:
    """Every valid event in one shard, in file order, plus a count of the lines that were not. A missing shard is empty. Blank lines are ignored; a trailing `\\r` from a hand edit is tolerated."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return ReadResult(events=(), malformed=0)
    events: list[Event] = []
    malformed = 0
    for number, raw_line in enumerate(raw.split(b"\n"), start=1):
        if not raw_line.strip():
            continue
        try:
            events.append(from_json(raw_line.rstrip(b"\r").decode(ENCODING)))
        except (UnicodeDecodeError, LedgerFormatError) as error:
            malformed += 1
            logger.warning("skipping malformed ledger line %s:%d: %s", path.name, number, error)
    return ReadResult(events=tuple(events), malformed=malformed)


def read_all() -> ReadResult:
    """Every shard in the ledger directory merged into one sequence, ordered by timestamp then id."""
    events: list[Event] = []
    malformed = 0
    for path in sorted(paths.ledger_dir().glob(f"*{SHARD_SUFFIX}")):
        result = read_shard(path)
        events.extend(result.events)
        malformed += result.malformed
    events.sort(key=lambda event: (event.ts, event.id))
    return ReadResult(events=tuple(events), malformed=malformed)
