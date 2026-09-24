"""The collector loop (roadmap section 3): records the collector's own presence as `collector_start` and `collector_stop` transitions, and proves it is alive between them with a per-tick checkpoint file, never a ledger line.

`clock` and `sleep` are injected. Tick spacing and the suspend check are measured against the clock, never by counting sleeps, so a test runs hundreds of ticks instantly. Nothing here knows what day it is: the loop writes UTC timestamps, which is what makes midnight and a daylight-saving change non-events.

Exit paths that write a clean `collector_stop`: the stop file, the stop event (set by a signal handler), and `max_ticks`. An unexpected exception writes no stop and leaves the checkpoint in place, so the next start records an inferred `unclean` stop at the last tick rather than a clean one that never happened.
"""

import json
import logging
import math
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import StrEnum

from timeexisting import paths
from timeexisting.collector import lockfile
from timeexisting.config.models import Config
from timeexisting.domain.clock import Clock
from timeexisting.ledger import store
from timeexisting.ledger.events import Confidence, Event, EventType, Source, profile_for

logger = logging.getLogger(__name__)

ROLE = "collector"
SUSPEND_FACTOR = 2
_SLEEP_SLICE = 0.5


class StartReason(StrEnum):
    LAUNCH = "launch"
    RESUMED = "resumed"


class StopReason(StrEnum):
    STOP_FILE = "stop_file"
    SIGNAL = "signal"
    MAX_TICKS = "max_ticks"
    UNCLEAN = "unclean"
    SUSPENDED = "suspended"


def _write(
    ts: datetime, host: str, profile: str, event: EventType, reason: str, confidence: Confidence
) -> None:
    store.append(
        Event.new(
            ts=ts,
            host=host,
            profile=profile,
            event=event,
            source=Source.COLLECTOR,
            confidence=confidence,
            data={"reason": reason},
        )
    )


def write_checkpoint(host: str, ts: datetime) -> bool:
    """Atomically replace the checkpoint with `ts` and our pid: write a temp file beside it, fsync, `os.replace`. A `PermissionError` (Windows, while something else holds the file) is logged and reported as `False`; the next tick tries again."""
    target = paths.checkpoint_file(host)
    temporary = target.with_name(f"{target.name}.tmp")
    payload = json.dumps({"ts": ts.isoformat(), "pid": os.getpid()}).encode("utf-8")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    except PermissionError as error:
        logger.warning("checkpoint not written, retrying next tick: %s", error)
        return False
    return True


def read_checkpoint(host: str) -> datetime | None:
    """The checkpoint's `ts`, or `None` if there is no checkpoint. Raises `ValueError` if it exists but does not parse to an aware timestamp."""
    try:
        raw = paths.checkpoint_file(host).read_bytes()
    except FileNotFoundError:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
        ts = datetime.fromisoformat(record["ts"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError(f"unparseable checkpoint: {error}") from error
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError(f"checkpoint ts is naive: {ts.isoformat()}")
    return ts


def delete_checkpoint(host: str) -> None:
    target = paths.checkpoint_file(host)
    target.unlink(missing_ok=True)
    target.with_name(f"{target.name}.tmp").unlink(missing_ok=True)


def _last_event(host: str) -> Event | None:
    events = store.read_shard(store.shard_path(host)).events
    return max(events, key=lambda event: (event.ts, event.id), default=None)


def recover(host: str, profile: str) -> None:
    """Close the previous run if it ended without a clean stop. Runs after the lock is held and before `collector_start`.

    A leftover checkpoint means the previous run may not have stopped cleanly. If the host's last ledger event is already a `collector_stop` at or after the checkpoint's `ts`, it did, and only the checkpoint's deletion was lost. Otherwise an inferred `collector_stop` with reason `unclean` goes in at the checkpoint's `ts`. An unparseable checkpoint is deleted and nothing is written: nothing is guessed.
    """
    try:
        checkpoint = read_checkpoint(host)
    except ValueError as error:
        logger.warning("deleting %s without recovery: %s", paths.checkpoint_file(host), error)
        delete_checkpoint(host)
        return
    if checkpoint is None:
        return
    last = _last_event(host)
    if last is not None and last.event is EventType.COLLECTOR_STOP and last.ts >= checkpoint:
        logger.info(
            "previous run stopped cleanly at %s; only its checkpoint was left behind", last.ts.isoformat()
        )
    else:
        logger.warning("previous run ended uncleanly; inferring collector_stop at %s", checkpoint.isoformat())
        _write(checkpoint, host, profile, EventType.COLLECTOR_STOP, StopReason.UNCLEAN, Confidence.INFERRED)
    delete_checkpoint(host)


def run_collector(
    clock: Clock,
    sleep: Callable[[float], object],
    cfg: Config,
    *,
    max_ticks: int | None = None,
    stop_event: threading.Event | None = None,
) -> StopReason:
    """Acquire the lock, recover, write `collector_start`, then tick every `cfg.collector.tick` until the stop file appears, `stop_event` is set, or `max_ticks` ticks have run. Returns the stop reason. Raises `lockfile.AlreadyRunning` or `lockfile.LockError` before writing anything if the lock cannot be taken."""
    stop_event = stop_event or threading.Event()
    host = paths.current_host()
    profile = profile_for(host, cfg)
    lock = lockfile.acquire(ROLE)
    try:
        if paths.stop_file().exists():
            logger.warning("removing a stop file left over from an earlier stop request")
            paths.stop_file().unlink(missing_ok=True)
        recover(host, profile)
        reason = _loop(clock, sleep, cfg, host, profile, max_ticks, stop_event)
        _write(clock.now(), host, profile, EventType.COLLECTOR_STOP, reason, Confidence.OBSERVED)
        delete_checkpoint(host)
        paths.stop_file().unlink(missing_ok=True)
        logger.info("collector stopped: %s", reason)
        return reason
    except Exception:
        logger.exception("collector failed; checkpoint left for recovery")
        raise
    finally:
        lockfile.release(lock)


def _loop(
    clock: Clock,
    sleep: Callable[[float], object],
    cfg: Config,
    host: str,
    profile: str,
    max_ticks: int | None,
    stop_event: threading.Event,
) -> StopReason:
    tick = cfg.collector.tick
    previous = clock.now()
    _write(previous, host, profile, EventType.COLLECTOR_START, StartReason.LAUNCH, Confidence.OBSERVED)
    write_checkpoint(host, previous)
    logger.info("collector started on %s, tick %s", host, tick)
    ticks = 0
    while True:
        if stop_event.is_set():
            return StopReason.SIGNAL
        if paths.stop_file().exists():
            return StopReason.STOP_FILE
        if max_ticks is not None and ticks >= max_ticks:
            return StopReason.MAX_TICKS
        sleep(tick.total_seconds())
        now = clock.now()
        ticks += 1
        if now < previous:
            logger.warning(
                "clock moved backwards from %s to %s; not a suspend", previous.isoformat(), now.isoformat()
            )
        elif now - previous > SUSPEND_FACTOR * tick:
            logger.info("clock jumped %s since the last tick: suspended", now - previous)
            _write(
                previous, host, profile, EventType.COLLECTOR_STOP, StopReason.SUSPENDED, Confidence.INFERRED
            )
            _write(now, host, profile, EventType.COLLECTOR_START, StartReason.RESUMED, Confidence.OBSERVED)
        write_checkpoint(host, now)
        previous = now


def interruptible_sleep(stop_event: threading.Event) -> Callable[[float], None]:
    """A `sleep` for production: sleeps in short slices and returns early once `stop_event` is set, so Ctrl+C is honoured within half a second rather than at the end of a 30-second tick."""

    def sleep(seconds: float) -> None:
        for _ in range(math.ceil(seconds / _SLEEP_SLICE)):
            if stop_event.is_set():
                return
            time.sleep(_SLEEP_SLICE)

    return sleep


@contextmanager
def signal_handlers(stop_event: threading.Event) -> Iterator[None]:
    """Route SIGINT, and SIGBREAK on Windows or SIGTERM elsewhere, to `stop_event` for the duration of the block, then restore whatever was installed before. Main thread only."""
    names = ["SIGINT", "SIGBREAK"] if os.name == "nt" else ["SIGINT", "SIGTERM"]
    previous = {}
    for name in names:
        number = getattr(signal, name)
        previous[number] = signal.signal(number, lambda _signum, _frame: stop_event.set())
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
