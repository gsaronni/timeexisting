"""Evidence for issue #10: the collector's own job object membership, decoded and put on one log line. Pure: no I/O, no clock, no Windows import. The calls that read the process live in `collector/jobinfo_win32.py` and are injected into `gather`, so the composition and its failure handling are tested on every platform.

Why this exists. A venv's or uv tool's `pythonw.exe` is a redirector launcher: the collector runs as its child under the base interpreter. CPython's launcher puts that child in a job object with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, and the launcher itself has no window and no console. The hypothesis behind issue #10 is that session end terminates the launcher without notice, the job closes, and the collector is killed before `WM_QUERYENDSESSION` reaches its hidden window. The line this module formats, written once at collector start, says whether the collector is in such a job and who its parent is. It changes no behaviour.
"""

from collections.abc import Callable
from dataclasses import dataclass

# Job object basic limit flags, `JOB_OBJECT_LIMIT_*` from the Windows SDK winnt.h, by bit. A bit not listed here is shown as hex by `decode_limit_flags`, never dropped.
LIMIT_FLAG_NAMES: dict[int, str] = {
    0x0001: "JOB_OBJECT_LIMIT_WORKINGSET",
    0x0002: "JOB_OBJECT_LIMIT_PROCESS_TIME",
    0x0004: "JOB_OBJECT_LIMIT_JOB_TIME",
    0x0008: "JOB_OBJECT_LIMIT_ACTIVE_PROCESS",
    0x0010: "JOB_OBJECT_LIMIT_AFFINITY",
    0x0020: "JOB_OBJECT_LIMIT_PRIORITY_CLASS",
    0x0040: "JOB_OBJECT_LIMIT_PRESERVE_JOB_TIME",
    0x0080: "JOB_OBJECT_LIMIT_SCHEDULING_CLASS",
    0x0100: "JOB_OBJECT_LIMIT_PROCESS_MEMORY",
    0x0200: "JOB_OBJECT_LIMIT_JOB_MEMORY",
    0x0400: "JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION",
    0x0800: "JOB_OBJECT_LIMIT_BREAKAWAY_OK",
    0x1000: "JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK",
    0x2000: "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
    0x4000: "JOB_OBJECT_LIMIT_SUBSET_AFFINITY",
}

_UNAVAILABLE = "unavailable"


def decode_limit_flags(flags: int) -> tuple[str, ...]:
    """The names of the bits set in `flags`, lowest bit first; a bit with no name is its hex value."""
    if flags < 0:
        raise ValueError(f"limit flags are an unsigned value, got {flags}")
    names: list[str] = []
    bit = 1
    remaining = flags
    while remaining:
        if remaining & 1:
            names.append(LIMIT_FLAG_NAMES.get(bit, f"{bit:#x}"))
        remaining >>= 1
        bit <<= 1
    return tuple(names)


@dataclass(frozen=True, slots=True)
class JobProbe:
    """What the probe found about this process. `executable` is `sys.executable`, the path Python reports, and `image` is the image the kernel runs for this pid: under a venv launcher the first is the launcher and the second the base interpreter. A field the probe could not read holds `unavailable: <reason>` instead of its value. `limit_flags` is `None` when the process is known to be outside any job."""

    pid: int
    executable: str
    image: str
    parent_pid: int | str
    parent_image: str
    in_job: bool | str
    limit_flags: int | str | None


def _unavailable(error: BaseException) -> str:
    return f"{_UNAVAILABLE}: {str(error) or type(error).__name__}"


def gather(
    pid: int,
    executable: str,
    *,
    parent_pid: Callable[[], int],
    image_path: Callable[[int], str],
    in_job: Callable[[], bool],
    limit_flags: Callable[[], int],
) -> JobProbe:
    """One `JobProbe` from the four calls, each of which may raise. A failure becomes that field's `unavailable: <reason>` and the rest are still read; nothing propagates, so the collector starts whatever Windows answers. `image_path` is read for `pid` itself and then for the parent, the latter only with a parent pid to read it for; the limit flags are skipped only when the process is known to be outside any job."""
    try:
        own_image = image_path(pid)
    except Exception as error:
        own_image = _unavailable(error)
    try:
        parent: int | str = parent_pid()
    except Exception as error:
        parent = _unavailable(error)
    if isinstance(parent, int):
        try:
            image = image_path(parent)
        except Exception as error:
            image = _unavailable(error)
    else:
        image = f"{_UNAVAILABLE}: no parent pid"
    try:
        member: bool | str = in_job()
    except Exception as error:
        member = _unavailable(error)
    flags: int | str | None
    if member is False:
        flags = None
    else:
        try:
            flags = limit_flags()
        except Exception as error:
            flags = _unavailable(error)
    return JobProbe(
        pid=pid,
        executable=executable,
        image=own_image,
        parent_pid=parent,
        parent_image=image,
        in_job=member,
        limit_flags=flags,
    )


def _yes_no(value: bool | str) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return value


def format_probe(probe: JobProbe) -> str:
    """The one INFO line: every field in a fixed order, the limit flags as hex with their decoded names, and no line break whatever the values hold."""
    parts = [
        f"pid {probe.pid}",
        f"executable {probe.executable}",
        f"image {probe.image}",
        f"parent pid {probe.parent_pid}",
        f"parent image {probe.parent_image}",
        f"in job {_yes_no(probe.in_job)}",
    ]
    if isinstance(probe.limit_flags, int):
        names = ", ".join(decode_limit_flags(probe.limit_flags)) or "none"
        parts.append(f"limit flags {probe.limit_flags:#x} ({names})")
    elif probe.limit_flags is not None:
        parts.append(f"limit flags {probe.limit_flags}")
    return " ".join(("job probe: " + ", ".join(parts)).splitlines())
