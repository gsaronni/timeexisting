"""The fix for issue #10, Windows half: the hook `__main__` runs before it imports anything else when `python -m timeexisting collect` starts. It reads this process's job through the ctypes seams of `collector/jobinfo_win32.py`, hands the facts to the pure `collector/respawn.py`, and either starts the base interpreter outside the job and exits, or logs why it stays. Imported only on Windows, only for a bare `collect`.

Cheap on purpose. The first process of every launch pays for this module, and the viewer gives a spawned collector five seconds to take its lock (`collector/spawn.py`), so the hook imports the ctypes seams, the pure decision and the logging setup, nothing more: no psutil, no pywin32, no `cli`.

Never raises into the collector. A seam that fails becomes `unavailable: <reason>` in the check line and a stay; a `Popen` that fails is logged and a stay; anything else is logged with its traceback and a stay. Only a successful respawn ends the process, with exit status 0 after the log handler is closed, so the launcher returns 0 and the respawned collector, outside the job, has the log file to itself.
"""

import ctypes
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from ctypes import wintypes

from timeexisting import logging_setup
from timeexisting.collector import jobinfo_win32
from timeexisting.collector.respawn import (
    GUARD_VARIABLE,
    ROLE,
    Decision,
    command,
    decide,
    environment,
    format_check,
)

logger = logging.getLogger(__name__)


def console_attached() -> bool:
    """Whether a console window is attached to this process (`GetConsoleWindow`): true for `te collect` in a terminal, false under `pythonw.exe`."""
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetConsoleWindow.argtypes = []
    api.GetConsoleWindow.restype = wintypes.HWND
    return bool(api.GetConsoleWindow())


def _read[T](call: Callable[[], T]) -> T | str:
    try:
        return call()
    except Exception as error:
        return f"unavailable: {str(error) or type(error).__name__}"


def _check(
    in_job: Callable[[], bool],
    limit_flags: Callable[[], int],
    console: Callable[[], bool],
    environ: Mapping[str, str],
    popen: Callable[..., object],
    executable: str,
    base_executable: str | None,
) -> Decision:
    member = _read(in_job)
    flags = None if member is False else _read(limit_flags)
    logger.info(format_check(os.getpid(), executable, member, flags))
    decision = decide(
        in_job=member,
        limit_flags=flags,
        console=bool(console()),
        guarded=GUARD_VARIABLE in environ,
        executable=executable,
        base_executable=base_executable,
    )
    if not decision.respawn:
        logger.info("respawn: %s", decision.reason)
        return decision
    assert base_executable is not None
    logger.info(
        "respawning outside the job: %s (%s, creation flags %#x)",
        base_executable,
        decision.reason,
        decision.creation_flags,
    )
    try:
        popen(
            command(base_executable),
            env=environment(environ, executable),
            creationflags=decision.creation_flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as error:
        logger.error("respawn failed, the collector carries on in its job: %s", error)
        return Decision(respawn=False, reason=f"respawn failed: {error}")
    return decision


def hook(
    argv: Sequence[str],
    *,
    in_job: Callable[[], bool] = jobinfo_win32.in_job,
    limit_flags: Callable[[], int] = jobinfo_win32.limit_flags,
    console: Callable[[], bool] = console_attached,
    environ: Mapping[str, str] | None = None,
    popen: Callable[..., object] = subprocess.Popen,
    executable: str = sys.executable,
    base_executable: str | None = None,
) -> Decision | None:
    """Run the check for a bare `collect`; `None` for any other `argv`, read nothing. Configures the collector's log first, so the check line and the decision land in the same file as the collector's own lines. On a respawn, closes that log and raises `SystemExit(0)`; on a stay, returns the decision and leaves the log configured, which `cli.main` replaces without stacking a handler."""
    if list(argv) != ["collect"]:
        return None
    if environ is None:
        environ = os.environ
    if base_executable is None:
        base_executable = getattr(sys, "_base_executable", None)
    logging_setup.configure(ROLE)
    try:
        decision = _check(in_job, limit_flags, console, environ, popen, executable, base_executable)
    except Exception:
        logger.exception("respawn check failed; the collector carries on in its job")
        return Decision(respawn=False, reason="respawn check failed")
    if not decision.respawn:
        return decision
    logging_setup.reset()
    raise SystemExit(0)
