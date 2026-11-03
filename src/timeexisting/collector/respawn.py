"""The fix for issue #10, pure half: whether a collector that finds itself inside a launcher's kill-on-close job starts the base interpreter outside it and exits, the command and environment of that respawn, and the line that records what it found. No I/O, no clock, no Windows import: `collector/respawn_win32.py` reads the process and runs the decision.

Why. Both launchers that start the collector run the base interpreter as a child inside a job object: CPython's `venvwlauncher.exe`, which a venv's `pythonw.exe` is, and uv's trampoline, which a tool install's is. Both set `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK` on that job, set `__PYVENV_LAUNCHER__` to their own path so `getpath` reports the venv as `sys.executable` and `sys.prefix`, and wait for the child. The launcher has no window and no console, so session end terminates it unheard, its job closes, and the collector is killed before `WM_QUERYENDSESSION` reaches its hidden window; recovery then reads the stop as `unclean`.

The hop. With silent breakaway, a child the collector creates is outside the job. So a collector in such a job starts `sys._base_executable -m timeexisting collect` with `__PYVENV_LAUNCHER__` set to its own `sys.executable`, which the interpreter cleared after reading it, and exits 0: the launcher returns, the job closes with nobody left in it, and the child reports the venv exactly as the first process did. `TIMEEXISTING_RESPAWNED` in the child's environment stops a second hop. With `BREAKAWAY_OK` alone the child asks for it with `CREATE_BREAKAWAY_FROM_JOB`. Without either the collector stays where it is and says so, as it does on a console run, which the user is watching, and whenever a fact about the process could not be read: a hop is never made on a guess.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from timeexisting.collector.jobinfo import decode_limit_flags

ROLE = "collector"
GUARD_VARIABLE = "TIMEEXISTING_RESPAWNED"
LAUNCHER_VARIABLE = "__PYVENV_LAUNCHER__"

# winnt.h: JOB_OBJECT_LIMIT_BREAKAWAY_OK, JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
BREAKAWAY_OK = 0x0800
SILENT_BREAKAWAY_OK = 0x1000
KILL_ON_JOB_CLOSE = 0x2000
# WinBase.h: #define CREATE_BREAKAWAY_FROM_JOB 0x01000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW: how the viewer spawns the collector (`collector/spawn.py`), and how the collector respawns itself. DETACHED_PROCESS already implies no console; CREATE_NO_WINDOW is kept for the case where the interpreter found is a console `python.exe`.
DETACHED_FLAGS = 0x00000008 | 0x00000200 | 0x08000000

_UNAVAILABLE = "unavailable"


def wants_hook(platform: str, argv: Sequence[str]) -> bool:
    """Whether `__main__` runs the hook at all: Windows, and a bare `collect`. The viewer, `collect status`, `collect stop` and the timeline never hop."""
    return platform == "win32" and list(argv) == ["collect"]


@dataclass(frozen=True, slots=True)
class Decision:
    """Respawn or stay, with the reason either way, and the creation flags of the respawn: `DETACHED_FLAGS`, plus `CREATE_BREAKAWAY_FROM_JOB` when the job allows breakaway only on request. Zero on a stay."""

    respawn: bool
    reason: str
    creation_flags: int = 0


def _stay(reason: str) -> Decision:
    return Decision(respawn=False, reason=reason)


def decide(
    *,
    in_job: bool | str,
    limit_flags: int | str | None,
    console: bool,
    guarded: bool,
    executable: str,
    base_executable: str | None,
) -> Decision:
    """The decision from what the process reads about itself. `in_job` and `limit_flags` are the probe's values, a string when a read failed; `console` says a console window is attached; `guarded` says `GUARD_VARIABLE` is set; `base_executable` is `sys._base_executable`, or `None` without one. A stay names its cause."""
    if console:
        return _stay("console run, respawn skipped")
    if guarded:
        return _stay(f"{GUARD_VARIABLE} is set, already respawned once, staying")
    if not isinstance(in_job, bool):
        return _stay(f"job membership {in_job}, staying")
    if not in_job:
        return _stay("not in a job")
    if not isinstance(limit_flags, int):
        flags = f"{_UNAVAILABLE}: not read" if limit_flags is None else limit_flags
        return _stay(f"limit flags {flags}, staying")
    if not limit_flags & KILL_ON_JOB_CLOSE:
        return _stay(f"job without KILL_ON_JOB_CLOSE ({limit_flags:#x}), staying")
    if not base_executable or base_executable == executable:
        return _stay(
            f"no base interpreter distinct from {executable} (sys._base_executable is {base_executable}), staying"
        )
    if limit_flags & SILENT_BREAKAWAY_OK:
        return Decision(
            respawn=True, reason="kill-on-close job with silent breakaway", creation_flags=DETACHED_FLAGS
        )
    if limit_flags & BREAKAWAY_OK:
        return Decision(
            respawn=True,
            reason="kill-on-close job with breakaway on request",
            creation_flags=DETACHED_FLAGS | CREATE_BREAKAWAY_FROM_JOB,
        )
    return _stay(f"kill-on-close job without breakaway ({limit_flags:#x}), staying")


def command(base_executable: str) -> list[str]:
    """The respawn: the base interpreter running this package's `collect`, as `collector/spawn.py` runs it."""
    return [base_executable, "-m", "timeexisting", "collect"]


def environment(environ: Mapping[str, str], launcher: str) -> dict[str, str]:
    """The child's environment: the parent's, plus `LAUNCHER_VARIABLE` naming `launcher` (this process's `sys.executable`), so the child reports the venv, and the guard. `environ` is not modified."""
    return {**environ, LAUNCHER_VARIABLE: launcher, GUARD_VARIABLE: "1"}


def _yes_no(value: bool | str) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return value


def format_check(pid: int, executable: str, in_job: bool | str, limit_flags: int | str | None) -> str:
    """The one INFO line the hook writes before it decides, in the shape of `jobinfo.format_probe` minus the fields it does not read: pid, executable, job membership and the flags as hex with their names. No line break whatever the values hold."""
    parts = [f"pid {pid}", f"executable {executable}", f"in job {_yes_no(in_job)}"]
    if isinstance(limit_flags, int):
        names = ", ".join(decode_limit_flags(limit_flags)) or "none"
        parts.append(f"limit flags {limit_flags:#x} ({names})")
    elif limit_flags is not None:
        parts.append(f"limit flags {limit_flags}")
    return " ".join(("respawn check: " + ", ".join(parts)).splitlines())
