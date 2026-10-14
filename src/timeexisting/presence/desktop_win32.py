"""The thin I/O half of the session probe (roadmap section 6): the only code that calls `WTSQuerySessionInformation`. Imported only on Windows, through `presence/backend.py`.

Both answers come from one record, `WTSINFOEX_LEVEL1_W`, queried with `WTSSessionInfoEx`: `SessionFlags` for the lock state and `LogonTime` for the logon. `OpenInputDesktop` is not used: on the work laptop it opens behind the lock screen (roadmap section 6).

Every function here is a seam: tests replace them. Each raises `OSError` on a failed call, which the caller logs. Nothing here asks for elevation.

Constants and the `WTSINFOEXW` layout are taken from the Windows SDK 10.0.26100 headers, cited beside each.
"""

import ctypes
from ctypes import wintypes
from datetime import UTC, datetime, timedelta

from timeexisting.presence.backend import SessionState

# WtsApi32.h: #define WTS_CURRENT_SERVER_HANDLE ((HANDLE)NULL)
WTS_CURRENT_SERVER_HANDLE = None
# WtsApi32.h: #define WTS_CURRENT_SESSION ((DWORD)-1)
WTS_CURRENT_SESSION = 0xFFFFFFFF
# WtsApi32.h: WTSSessionInfoEx is member 25 of enum _WTS_INFO_CLASS (WTSInitialProgram is 0).
WTS_SESSION_INFO_EX = 25
# WtsApi32.h: #define WINSTATIONNAME_LENGTH 32, #define DOMAIN_LENGTH 17, #define USERNAME_LENGTH 20
WINSTATIONNAME_LENGTH = 32
DOMAIN_LENGTH = 17
USERNAME_LENGTH = 20
# WtsApi32.h: #define WTS_SESSIONSTATE_UNKNOWN 0xFFFFFFFF, WTS_SESSIONSTATE_LOCK 0x00000000, WTS_SESSIONSTATE_UNLOCK 0x00000001
WTS_SESSIONSTATE_UNKNOWN = 0xFFFFFFFF
WTS_SESSIONSTATE_LOCK = 0x00000000
WTS_SESSIONSTATE_UNLOCK = 0x00000001
# The only level WtsApi32.h defines for WTSINFOEXW.Data.
WTSINFOEX_LEVEL1 = 1

_STATES = {WTS_SESSIONSTATE_LOCK: SessionState.LOCKED, WTS_SESSIONSTATE_UNLOCK: SessionState.UNLOCKED}

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)


class WTSINFOEX_LEVEL1_W(ctypes.Structure):
    """WtsApi32.h `typedef struct _WTSINFOEX_LEVEL1_W`. `SessionState` is the `WTS_CONNECTSTATE_CLASS` enum; `SessionFlags` is a `LONG`, so `WTS_SESSIONSTATE_UNKNOWN` reads back as -1; each `LARGE_INTEGER` is a FILETIME count of 100 ns since 1601-01-01 UTC."""

    _fields_ = (
        ("SessionId", wintypes.ULONG),
        ("SessionState", ctypes.c_int),
        ("SessionFlags", wintypes.LONG),
        ("WinStationName", wintypes.WCHAR * (WINSTATIONNAME_LENGTH + 1)),
        ("UserName", wintypes.WCHAR * (USERNAME_LENGTH + 1)),
        ("DomainName", wintypes.WCHAR * (DOMAIN_LENGTH + 1)),
        ("LogonTime", ctypes.c_longlong),
        ("ConnectTime", ctypes.c_longlong),
        ("DisconnectTime", ctypes.c_longlong),
        ("LastInputTime", ctypes.c_longlong),
        ("CurrentTime", ctypes.c_longlong),
        ("IncomingBytes", wintypes.DWORD),
        ("OutgoingBytes", wintypes.DWORD),
        ("IncomingFrames", wintypes.DWORD),
        ("OutgoingFrames", wintypes.DWORD),
        ("IncomingCompressedBytes", wintypes.DWORD),
        ("OutgoingCompressedBytes", wintypes.DWORD),
    )


class WTSINFOEX_LEVEL_W(ctypes.Union):
    """WtsApi32.h `typedef union _WTSINFOEX_LEVEL_W`."""

    _fields_ = (("WTSInfoExLevel1", WTSINFOEX_LEVEL1_W),)


class WTSINFOEXW(ctypes.Structure):
    """WtsApi32.h `typedef struct _WTSINFOEXW`."""

    _fields_ = (("Level", wintypes.DWORD), ("Data", WTSINFOEX_LEVEL_W))


_wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
_wtsapi32.WTSQuerySessionInformationW.argtypes = (
    wintypes.HANDLE,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(wintypes.DWORD),
)
_wtsapi32.WTSQuerySessionInformationW.restype = wintypes.BOOL
_wtsapi32.WTSFreeMemory.argtypes = (ctypes.c_void_p,)
_wtsapi32.WTSFreeMemory.restype = None


def filetime_to_utc(value: int) -> datetime | None:
    """A FILETIME count as aware UTC, or `None` for zero, which WTS uses for a time that is not set."""
    if value <= 0:
        return None
    return _FILETIME_EPOCH + timedelta(microseconds=value // 10)


def session_info() -> WTSINFOEX_LEVEL1_W:
    """This session's `WTSINFOEX_LEVEL1_W`, copied out of the WTS buffer before it is freed. Raises `OSError` if the query fails, returns too few bytes, or carries a level other than 1."""
    buffer = ctypes.c_void_p()
    returned = wintypes.DWORD()
    if not _wtsapi32.WTSQuerySessionInformationW(
        WTS_CURRENT_SERVER_HANDLE,
        WTS_CURRENT_SESSION,
        WTS_SESSION_INFO_EX,
        ctypes.byref(buffer),
        ctypes.byref(returned),
    ):
        error = ctypes.get_last_error()
        raise OSError(error, f"WTSQuerySessionInformationW failed: {ctypes.FormatError(error)}")
    try:
        if returned.value < ctypes.sizeof(WTSINFOEXW):
            raise OSError(f"WTSQuerySessionInformationW returned {returned.value} bytes for WTSINFOEXW")
        info = ctypes.cast(buffer, ctypes.POINTER(WTSINFOEXW)).contents
        if info.Level != WTSINFOEX_LEVEL1:
            raise OSError(f"WTSINFOEXW level {info.Level}, expected {WTSINFOEX_LEVEL1}")
        return WTSINFOEX_LEVEL1_W.from_buffer_copy(info.Data.WTSInfoExLevel1)
    finally:
        _wtsapi32.WTSFreeMemory(buffer)


def session_state() -> SessionState | None:
    """This session's lock state from `SessionFlags`, or `None` for `WTS_SESSIONSTATE_UNKNOWN`. Raises `OSError` if the query fails or the flags hold a value the header does not define."""
    flags = session_info().SessionFlags & 0xFFFFFFFF
    if flags == WTS_SESSIONSTATE_UNKNOWN:
        return None
    state = _STATES.get(flags)
    if state is None:
        raise OSError(f"SessionFlags {flags:#x} is not a WTS_SESSIONSTATE value")
    return state


def logon_time() -> datetime | None:
    """This session's logon time from the same record, aware UTC, or `None` when WTS reports none. Raises `OSError` if the query fails."""
    return filetime_to_utc(session_info().LogonTime)
