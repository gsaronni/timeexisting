"""The viewer's side of the collector lifecycle (roadmap section 3): when no collector holds the lock, start one detached and wait briefly for its lock to appear.

The collector is identified only through `lockfile.status`, never through the `Popen` pid. A venv's `pythonw.exe` is a launcher, and the real collector is its child process with a different pid.
"""

import logging
import math
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from timeexisting.collector import lockfile
from timeexisting.collector.daemon import ROLE

logger = logging.getLogger(__name__)

SPAWN_TIMEOUT = 3.0
_POLL_SECONDS = 0.25

# DETACHED_PROCESS already implies no console; CREATE_NO_WINDOW is kept for the case where the interpreter found is a console `python.exe`.
_WINDOWS_FLAGS = (
    0x00000008 | 0x00000200 | 0x08000000
)  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW


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


def ensure_running(
    *,
    spawner: Callable[[], object] | None = None,
    sleep: Callable[[float], object] = time.sleep,
    timeout: float = SPAWN_TIMEOUT,
) -> lockfile.LockStatus | None:
    """The live collector's lock status. A running collector is returned as found. Otherwise one is spawned and its lock polled for up to `timeout` seconds; `None` means it did not appear in time, and the viewer carries on without it."""
    owner = lockfile.status(ROLE)
    if owner is not None:
        logger.info("collector already running: pid %d", owner.pid)
        return owner
    try:
        (spawner or spawn)()
    except OSError:
        logger.exception("could not spawn the collector")
        return None
    for _ in range(math.ceil(timeout / _POLL_SECONDS)):
        sleep(_POLL_SECONDS)
        owner = lockfile.status(ROLE)
        if owner is not None:
            logger.info("collector up: pid %d", owner.pid)
            return owner
    logger.warning("spawned collector did not take the lock within %.1fs", timeout)
    return None
