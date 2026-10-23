"""The thin I/O half of the input probe (roadmap section 6): the only code that calls `GetLastInputInfo`. Imported only on Windows, through `presence/backend.py`.

`last_input` is a seam: tests replace it. It raises `OSError` on a failed call, which the caller logs. Neither call needs elevation: `GetLastInputInfo` reports the calling session's own input.

The `LASTINPUTINFO` layout is taken from the Windows SDK 10.0.26100 headers, cited beside it.
"""

import ctypes
from ctypes import wintypes


class LASTINPUTINFO(ctypes.Structure):
    """WinUser.h `typedef struct tagLASTINPUTINFO { UINT cbSize; DWORD dwTime; }`. `dwTime` is the `GetTickCount` value at the last input."""

    _fields_ = (("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD))


_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.GetLastInputInfo.argtypes = (ctypes.POINTER(LASTINPUTINFO),)
_user32.GetLastInputInfo.restype = wintypes.BOOL
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
# The 32-bit `GetTickCount`, not `GetTickCount64`: `dwTime` is 32-bit, so both wrap together.
_kernel32.GetTickCount.argtypes = ()
_kernel32.GetTickCount.restype = wintypes.DWORD


def last_input() -> tuple[int, int]:
    """`LASTINPUTINFO.dwTime` and `GetTickCount()`, read one straight after the other. Raises `OSError` if `GetLastInputInfo` fails."""
    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    if not _user32.GetLastInputInfo(ctypes.byref(info)):
        error = ctypes.get_last_error()
        raise OSError(error, f"GetLastInputInfo failed: {ctypes.FormatError(error)}")
    return int(info.dwTime), int(_kernel32.GetTickCount())
