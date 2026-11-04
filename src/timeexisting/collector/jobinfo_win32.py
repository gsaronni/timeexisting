"""The Windows I/O half of the job object probe (issue #10 evidence): the only code that asks Windows about this process's parent and job. Imported only on Windows, by `collector/session_win32.py`.

Follows `presence/eventlog_win32.py`: every function here is a seam that raises `JobInfoUnavailable` on failure and parses nothing, and `probe` hands them to the pure `jobinfo.gather`, which turns each failure into a field value. `in_job` and `limit_flags` are also read by the respawn hook (`collector/respawn_win32.py`) before anything else is imported, so this module imports nothing beyond ctypes at module level. Nothing here asks for elevation: the parent is opened with `PROCESS_QUERY_LIMITED_INFORMATION`, which a user has on their own processes, and the job queries are about the calling process's own job. `ctypes` is used because pywin32's `win32job` cannot query the current job through a null handle.
"""

import ctypes
import functools
import os
import sys
from ctypes import wintypes

from timeexisting.collector.jobinfo import JobProbe, gather

# processthreadsapi.h: #define PROCESS_QUERY_LIMITED_INFORMATION 0x1000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
# winnt.h: JOBOBJECTINFOCLASS JobObjectExtendedLimitInformation = 9
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
# The longest path `QueryFullProcessImageNameW` can return, in characters.
_PATH_CHARS = 32768


class JobInfoUnavailable(RuntimeError):
    """A fact about the process that Windows would not give: the call failed, or the parent could not be opened. Raised by this layer, never by `gather`."""


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


@functools.cache
def _kernel32() -> ctypes.WinDLL:
    """kernel32 with the prototypes this module calls, loaded on first use rather than at import."""
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    api.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        wintypes.PDWORD,
    ]
    api.QueryFullProcessImageNameW.restype = wintypes.BOOL
    api.GetCurrentProcess.argtypes = []
    api.GetCurrentProcess.restype = wintypes.HANDLE
    api.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.PBOOL]
    api.IsProcessInJob.restype = wintypes.BOOL
    api.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPDWORD,
    ]
    api.QueryInformationJobObject.restype = wintypes.BOOL
    return api


def _failure(function: str) -> JobInfoUnavailable:
    code = ctypes.get_last_error()
    return JobInfoUnavailable(f"{function} failed, {code} {ctypes.FormatError(code).strip()}")


def parent_pid() -> int:
    """The pid of this process's parent, as the kernel records it; the parent may already be gone. psutil is imported here, on first use, because the respawn hook (`collector/respawn_win32.py`) imports this module before anything else and reads only the ctypes seams."""
    import psutil

    try:
        return psutil.Process(os.getpid()).ppid()
    except psutil.Error as error:
        raise JobInfoUnavailable(f"ppid: {error}") from error


def image_path(pid: int) -> str:
    """The full image path of process `pid`, opened with `PROCESS_QUERY_LIMITED_INFORMATION` only."""
    api = _kernel32()
    handle = api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        raise _failure("OpenProcess")
    try:
        size = wintypes.DWORD(_PATH_CHARS)
        buffer = ctypes.create_unicode_buffer(_PATH_CHARS)
        if not api.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise _failure("QueryFullProcessImageNameW")
        return buffer.value
    finally:
        api.CloseHandle(handle)


def in_job() -> bool:
    """Whether this process is associated with any job (`IsProcessInJob` with a null job handle)."""
    api = _kernel32()
    result = wintypes.BOOL()
    if not api.IsProcessInJob(api.GetCurrentProcess(), None, ctypes.byref(result)):
        raise _failure("IsProcessInJob")
    return bool(result.value)


def limit_flags() -> int:
    """The `LimitFlags` of this process's own job, the immediate one when jobs are nested (`QueryInformationJobObject` with a null job handle). Fails when the process is in no job."""
    api = _kernel32()
    info = _ExtendedLimitInformation()
    returned = wintypes.DWORD()
    if not api.QueryInformationJobObject(
        None,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(info),
        ctypes.sizeof(info),
        ctypes.byref(returned),
    ):
        raise _failure("QueryInformationJobObject")
    return int(info.BasicLimitInformation.LimitFlags)


def probe() -> JobProbe:
    """This process's `JobProbe`, every field read through the seams above and every failure a field value. Never raises on a failed call; `gather` sees to that."""
    return gather(
        os.getpid(),
        sys.executable,
        parent_pid=parent_pid,
        image_path=image_path,
        in_job=in_job,
        limit_flags=limit_flags,
    )
