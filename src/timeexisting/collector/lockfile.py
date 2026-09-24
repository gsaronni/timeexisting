"""Single-instance lockfile per role and host (roadmap section 3): `state/<role>-<host>.lock`, created atomically, holding the owner's pid, host, role, process start time and lock schema.

A lock is live only if its pid exists on this host and that process started when the lock says it did, within one second, so a recycled pid cannot masquerade as the owner. Anything else is stale and is reclaimed. A lock recorded by another host cannot be checked from here and is treated as live: it is never deleted on a guess. The filename already carries the host, so this is a defensive check against a file copied or synced into the wrong place.

A lock that cannot be read is stale only once its mtime is more than `UNREADABLE_GRACE` old. A younger one is most likely another process between creating the file and writing it, so it is left alone.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psutil

from timeexisting import paths
from timeexisting.domain.clock import Clock, SystemClock
from timeexisting.paths import current_host

logger = logging.getLogger(__name__)

LOCK_SCHEMA = 1
CREATE_TIME_TOLERANCE = 1.0
UNREADABLE_GRACE = 5.0

# Windows opens descriptors in text mode unless told otherwise.
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)


@dataclass(frozen=True, slots=True)
class LockStatus:
    """What a lockfile records. `create_time` is the owner's process start time in epoch seconds, as `psutil` reports it."""

    pid: int
    host: str
    role: str
    create_time: float
    schema: int

    @property
    def started(self) -> datetime:
        return datetime.fromtimestamp(self.create_time, UTC)


@dataclass(frozen=True, slots=True)
class Lock:
    """A lock this process holds. Pass it back to `release`."""

    path: Path
    status: LockStatus


class AlreadyRunning(RuntimeError):
    """Another live process holds the lock for this role."""

    def __init__(self, status: LockStatus) -> None:
        super().__init__(
            f"{status.role} already running: pid {status.pid}, host {status.host}, started {status.started.isoformat()}"
        )
        self.status = status
        self.pid = status.pid
        self.started = status.started


class LockError(RuntimeError):
    """The lock could not be taken for a reason other than a live owner: an unreadable lock too young to call stale, or losing a reclaim race twice."""


def _own_status(role: str) -> LockStatus:
    process = psutil.Process()
    return LockStatus(
        pid=process.pid,
        host=current_host(),
        role=role,
        create_time=process.create_time(),
        schema=LOCK_SCHEMA,
    )


def _encode(status: LockStatus) -> bytes:
    record = {
        "pid": status.pid,
        "host": status.host,
        "role": status.role,
        "create_time": status.create_time,
        "schema": status.schema,
    }
    return json.dumps(record).encode("utf-8")


def _read(path: Path) -> LockStatus | None:
    """The recorded status, or `None` when the file is missing or does not parse."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        record = json.loads(raw.decode("utf-8"))
        status = LockStatus(
            pid=record["pid"],
            host=record["host"],
            role=record["role"],
            create_time=record["create_time"],
            schema=record["schema"],
        )
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        logger.warning("unreadable lockfile %s: %s", path, error)
        return None
    valid = (
        type(status.pid) is int
        and isinstance(status.host, str)
        and isinstance(status.role, str)
        and type(status.create_time) in (int, float)
        and type(status.schema) is int
    )
    if not valid:
        logger.warning("unreadable lockfile %s: unexpected field types", path)
        return None
    return status


def _is_live(status: LockStatus) -> bool:
    if status.host != current_host():
        return True
    if not psutil.pid_exists(status.pid):
        return False
    try:
        create_time = psutil.Process(status.pid).create_time()
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        # Our own collector always runs as us; a process we may not inspect is someone else's.
        return False
    return abs(create_time - status.create_time) <= CREATE_TIME_TOLERANCE


def _is_ours(recorded: LockStatus, ours: LockStatus) -> bool:
    return (
        recorded.pid == ours.pid
        and recorded.host == ours.host
        and abs(recorded.create_time - ours.create_time) <= CREATE_TIME_TOLERANCE
    )


def _try_create(path: Path, status: LockStatus) -> bool:
    try:
        descriptor = os.open(path, _CREATE_FLAGS, 0o644)
    except FileExistsError:
        return False
    try:
        os.write(descriptor, _encode(status))
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    return True


def _age(path: Path, clock: Clock) -> float | None:
    """Seconds since `path` was last modified, or `None` if it has gone."""
    try:
        modified = path.stat().st_mtime
    except FileNotFoundError:
        return None
    return clock.now().timestamp() - modified


def acquire(role: str, *, clock: Clock | None = None) -> Lock:
    """Take the lock for `role`, or raise `AlreadyRunning` if a live process holds it. A stale lock is removed and creation retried once. An unreadable lock no older than `UNREADABLE_GRACE` raises `LockError` and is left in place. `clock` only dates that unreadable lock."""
    clock = clock or SystemClock()
    path = paths.lock_file(role)
    ours = _own_status(role)
    for attempt in range(2):
        if _try_create(path, ours):
            logger.info("acquired %s lock, pid %d", role, ours.pid)
            return Lock(path=path, status=ours)
        recorded = _read(path)
        if recorded is not None and _is_live(recorded):
            raise AlreadyRunning(recorded)
        if recorded is None:
            age = _age(path, clock)
            if age is not None and age <= UNREADABLE_GRACE:
                raise LockError(
                    f"{role} lock at {path} is unreadable and {age:.1f}s old: another process is still writing it"
                )
        if attempt == 0:
            logger.warning("reclaiming stale %s lock: %s", role, recorded)
            path.unlink(missing_ok=True)
    raise LockError(f"could not acquire {role} lock at {path}: it reappeared after a stale reclaim")


def release(lock: Lock) -> None:
    """Remove the lockfile, but only if it still records this lock's owner. A lock another process has since taken is left alone."""
    recorded = _read(lock.path)
    if recorded is None:
        return
    if not _is_ours(recorded, lock.status):
        logger.warning("not releasing %s: held by pid %d on %s", lock.path, recorded.pid, recorded.host)
        return
    lock.path.unlink(missing_ok=True)
    logger.info("released %s lock, pid %d", lock.status.role, lock.status.pid)


def status(role: str) -> LockStatus | None:
    """The live owner of `role`'s lock, or `None` when there is none, including when the lock cannot be read. Read-only: a stale lock is reported as `None` and left for `acquire` to reclaim."""
    recorded = _read(paths.lock_file(role))
    if recorded is None or not _is_live(recorded):
        return None
    return recorded
