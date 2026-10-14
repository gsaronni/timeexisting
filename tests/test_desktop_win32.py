"""The thin Windows I/O layer of the session probe. `wtsapi32` is replaced: no test touches the real session."""

import ctypes
import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

if sys.platform != "win32":
    pytest.skip("the session probe seams load wtsapi32", allow_module_level=True)

from timeexisting.presence import desktop_win32
from timeexisting.presence.backend import SessionState

_LOGON = datetime(2026, 9, 17, 5, 58, 12, 345678, tzinfo=UTC)
# 100 ns ticks from 1601-01-01 UTC to _LOGON.
_LOGON_FILETIME = 134_340_982_923_456_780


def test_the_wtsinfoexw_layout_matches_the_header():
    """WtsApi32.h: a DWORD level, then the level 1 record aligned to 8: ULONG, the connect state enum, LONG, WCHAR[33], WCHAR[21], WCHAR[18], five LARGE_INTEGERs and six DWORDs."""
    assert ctypes.sizeof(desktop_win32.WTSINFOEXW) == 232
    assert desktop_win32.WTSINFOEXW.Data.offset == 8
    assert ctypes.sizeof(desktop_win32.WTSINFOEX_LEVEL1_W) == 224
    assert desktop_win32.WTSINFOEX_LEVEL1_W.SessionFlags.offset == 8
    assert desktop_win32.WTSINFOEX_LEVEL1_W.LogonTime.offset == 160


def test_constants_match_the_headers():
    assert desktop_win32.WTS_CURRENT_SERVER_HANDLE is None
    assert desktop_win32.WTS_CURRENT_SESSION == 0xFFFFFFFF
    assert desktop_win32.WTS_SESSION_INFO_EX == 25
    assert desktop_win32.WTS_SESSIONSTATE_UNKNOWN == 0xFFFFFFFF
    assert desktop_win32.WTS_SESSIONSTATE_LOCK == 0x00000000
    assert desktop_win32.WTS_SESSIONSTATE_UNLOCK == 0x00000001
    assert desktop_win32.WTSINFOEX_LEVEL1 == 1


def test_filetime_converts_to_aware_utc():
    assert desktop_win32.filetime_to_utc(_LOGON_FILETIME) == _LOGON
    assert desktop_win32.filetime_to_utc(_LOGON_FILETIME).tzinfo is UTC


def test_a_zero_filetime_is_no_time():
    assert desktop_win32.filetime_to_utc(0) is None


class _FakeWtsApi:
    """Answers `WTSQuerySessionInformationW` with a `WTSINFOEXW` it owns, writing through the `byref` arguments."""

    def __init__(
        self,
        flags: int = 1,
        logon: int = _LOGON_FILETIME,
        level: int = 1,
        ok: bool = True,
        size: int | None = None,
    ) -> None:
        self.info = desktop_win32.WTSINFOEXW(Level=level)
        self.info.Data.WTSInfoExLevel1.SessionFlags = flags
        self.info.Data.WTSInfoExLevel1.LogonTime = logon
        self.ok = ok
        self.size = ctypes.sizeof(self.info) if size is None else size
        self.queries: list[tuple] = []
        self.freed: list[int | None] = []

    def WTSQuerySessionInformationW(self, server, session, info_class, buffer, returned):
        self.queries.append((server, session, info_class))
        if not self.ok:
            return False
        buffer._obj.value = ctypes.addressof(self.info)
        returned._obj.value = self.size
        return True

    def WTSFreeMemory(self, pointer):
        self.freed.append(pointer.value)


def _api(monkeypatch, **kwargs) -> _FakeWtsApi:
    fake = _FakeWtsApi(**kwargs)
    monkeypatch.setattr(
        desktop_win32,
        "_wtsapi32",
        SimpleNamespace(
            WTSQuerySessionInformationW=fake.WTSQuerySessionInformationW, WTSFreeMemory=fake.WTSFreeMemory
        ),
    )
    return fake


@pytest.mark.parametrize(("flags", "state"), [(0, SessionState.LOCKED), (1, SessionState.UNLOCKED)])
def test_session_flags_read_as_the_lock_state(monkeypatch, flags, state):
    fake = _api(monkeypatch, flags=flags)
    assert desktop_win32.session_state() is state
    assert fake.queries == [(None, 0xFFFFFFFF, 25)]
    assert fake.freed == [ctypes.addressof(fake.info)]


def test_an_unknown_session_state_reads_none(monkeypatch):
    _api(monkeypatch, flags=-1)  # WTS_SESSIONSTATE_UNKNOWN in a LONG
    assert desktop_win32.session_state() is None


def test_session_flags_outside_the_header_raise(monkeypatch):
    _api(monkeypatch, flags=2)
    with pytest.raises(OSError, match="SessionFlags 0x2"):
        desktop_win32.session_state()


def test_the_logon_time_is_read_from_the_same_record(monkeypatch):
    fake = _api(monkeypatch, flags=0)
    assert desktop_win32.logon_time() == _LOGON
    assert fake.queries == [(None, 0xFFFFFFFF, 25)]
    assert fake.freed == [ctypes.addressof(fake.info)]


def test_no_logon_time_reads_none(monkeypatch):
    _api(monkeypatch, logon=0)
    assert desktop_win32.logon_time() is None


def test_the_record_is_copied_out_before_the_buffer_is_freed(monkeypatch):
    fake = _api(monkeypatch, flags=1)
    info = desktop_win32.session_info()
    fake.info.Data.WTSInfoExLevel1.SessionFlags = 0
    assert info.SessionFlags == 1


def test_a_failed_query_raises_os_error(monkeypatch):
    fake = _api(monkeypatch, ok=False)
    with pytest.raises(OSError, match="WTSQuerySessionInformationW failed"):
        desktop_win32.session_state()
    assert fake.freed == []


def test_a_short_buffer_raises_and_is_still_freed(monkeypatch):
    fake = _api(monkeypatch, size=8)
    with pytest.raises(OSError, match="returned 8 bytes"):
        desktop_win32.logon_time()
    assert fake.freed == [ctypes.addressof(fake.info)]


def test_a_level_other_than_1_raises_and_is_still_freed(monkeypatch):
    fake = _api(monkeypatch, level=2)
    with pytest.raises(OSError, match="level 2"):
        desktop_win32.session_state()
    assert fake.freed == [ctypes.addressof(fake.info)]
