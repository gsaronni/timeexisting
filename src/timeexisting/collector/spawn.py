"""The viewer's side of the collector lifecycle (roadmap section 3): when no collector holds the lock, start one detached, wait briefly for its lock to appear, and then for its first reconciliation, so the start the viewer resolves next reads the gap the collector backfills.

The collector is identified only through `lockfile.status`, never through the `Popen` pid. A venv's `pythonw.exe` is a launcher, and the real collector is its child process with a different pid.

The lock is taken before the backfill: a spawned collector acquires its lock, recovers, writes `collector_start` and only then reads the event logs for the gap since its last run (`daemon._loop`). On 28 October 2026 the viewer resolved the start within 30 ms of the lock, read a ledger without the morning's unlock, and prompted. So the viewer now waits for the checkpoint the collector writes after that first read, the first one of the run that holds the read cursors (`daemon.reconciled_checkpoint`), under a bounded timeout; on the timeout it carries on and resolves the start as the ledger stands.
"""

import logging
import math
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from timeexisting.collector import lockfile
from timeexisting.collector.daemon import ROLE, reconciled_checkpoint

logger = logging.getLogger(__name__)

# How long a spawned collector gets to take its lock. The interpreter's start under `pythonw.exe` took 3.0 to 3.9 s on the dev host at step 10, so the earlier 3.0 s missed two launches out of four and the footer reported a collector that had in fact come up.
SPAWN_TIMEOUT = 5.0
# How long a spawned collector gets to finish its first reconciliation once its lock is up. The first read of a 17-hour gap took about 0.3 s on the dev host; a gap of weeks reads more.
SETTLE_TIMEOUT = 10.0
_POLL_SECONDS = 0.25

# DETACHED_PROCESS already implies no console; CREATE_NO_WINDOW is kept for the case where the interpreter found is a console `python.exe`.
_WINDOWS_FLAGS = (
    0x00000008 | 0x00000200 | 0x08000000
)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


@dataclass(frozen=True, slots=True)
class Ensured:
    """What `ensure_running` found or did. `owner` is the live collector's lock, or `None` when one was spawned and never took its lock. `spawned` says this viewer started it, as opposed to finding it running."""

    owner: lockfile.LockStatus | None
    spawned: bool

    @property
    def failed(self) -> bool:
        """A collector was spawned and never took its lock."""
        return self.spawned and self.owner is None


def _interpreter(platform: str, executable: str) -> str:
    """`pythonw.exe` beside the running interpreter on Windows, so the collector has no console; the running interpreter everywhere else, or when `pythonw.exe` is missing."""
    if platform != "win32":
        return executable
    windowless = Path(executable).with_name("pythonw.exe")
    if windowless.is_file():
        return str(windowless)
    logger.warning("no pythonw.exe beside %s, spawning the collector with it instead", executable)
    return executable


def command(platform: str = sys.platform, executable: str = sys.executable) -> list[str]:
    return [_interpreter(platform, executable), "-m", "timeexisting", "collect"]


def spawn(*, platform: str = sys.platform, popen: Callable[..., object] = subprocess.Popen) -> None:
    """Start `te collect` detached from this process and its console, with every standard stream on `DEVNULL`. Detaching is the caller's job, never the collector's."""
    argv = command(platform)
    options: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if platform == "win32":
        options["creationflags"] = _WINDOWS_FLAGS
    else:
        options["start_new_session"] = True
    popen(argv, **options)
    logger.info("spawned collector: %s", argv)


def wait_for_reconciliation(
    owner: lockfile.LockStatus,
    *,
    after: datetime | None = None,
    timeout: float,
    sleep: Callable[[float], object] = time.sleep,
) -> datetime | None:
    """Poll, every quarter second for up to `timeout` seconds, for a checkpoint written by `owner`'s collector after a reconciliation (`daemon.reconciled_checkpoint`), and return its tick; with `after`, only a checkpoint from a later tick counts, which is how the viewer waits for a running collector's next tick. The checkpoint is read before the first sleep, so a tick already on disk costs no wait. `None` when the timeout passes first."""

    def reconciled() -> datetime | None:
        stamp = reconciled_checkpoint(owner.host, owner.pid)
        return stamp if stamp is not None and (after is None or stamp > after) else None

    for _ in range(math.ceil(timeout / _POLL_SECONDS)):
        if (stamp := reconciled()) is not None:
            return stamp
        sleep(_POLL_SECONDS)
    return reconciled()


def ensure_running(
    *,
    spawner: Callable[[], object] | None = None,
    sleep: Callable[[float], object] = time.sleep,
    timeout: float = SPAWN_TIMEOUT,
    settle_timeout: float = SETTLE_TIMEOUT,
) -> Ensured:
    """The live collector. A running collector is returned as found, at once. Otherwise one is spawned and its lock polled for up to `timeout` seconds; once the lock is up, its first reconciliation is waited for up to `settle_timeout` seconds more, so the ledger the viewer reads next holds the gap's records. A collector that never takes its lock, or never settles, is logged, and the viewer carries on without waiting further."""
    owner = lockfile.status(ROLE)
    if owner is not None:
        logger.info("collector already running: pid %d", owner.pid)
        return Ensured(owner, spawned=False)
    try:
        (spawner or spawn)()
    except OSError:
        logger.exception("could not spawn the collector")
        return Ensured(None, spawned=True)
    for _ in range(math.ceil(timeout / _POLL_SECONDS)):
        sleep(_POLL_SECONDS)
        owner = lockfile.status(ROLE)
        if owner is not None:
            break
    else:
        logger.warning("spawned collector did not take the lock within %.1fs", timeout)
        return Ensured(None, spawned=True)
    logger.info("collector up: pid %d", owner.pid)
    settled = wait_for_reconciliation(owner, timeout=settle_timeout, sleep=sleep)
    if settled is None:
        logger.warning(
            "spawned collector did not finish its first reconciliation within %.1fs; reading the ledger as it stands",
            settle_timeout,
        )
    else:
        logger.info("collector reconciled at %s", settled.isoformat())
    return Ensured(owner, spawned=True)
