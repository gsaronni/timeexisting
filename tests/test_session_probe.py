"""The session probe backend: the registry and the reader that joins the two Windows seams. Every seam is a fake: no test touches the real session."""

import logging
import sys
from datetime import UTC, datetime

import pytest

from timeexisting.presence import backend
from timeexisting.presence.backend import NoSessionProbe, SessionState, Win32Session, session_probe

_LOGON = datetime(2026, 9, 17, 5, 58, 12, tzinfo=UTC)


def _fail() -> SessionState | None:
    raise OSError(5, "Access is denied.")


@pytest.mark.parametrize("state", [SessionState.LOCKED, SessionState.UNLOCKED, None])
def test_the_state_comes_from_the_session_flags_seam(state):
    assert Win32Session(lambda: state, lambda: _LOGON).state() is state


def test_a_failing_session_flags_seam_is_logged_and_reads_unknown(caplog):
    with caplog.at_level(logging.WARNING, logger=backend.__name__):
        assert Win32Session(_fail, lambda: _LOGON).state() is None
    assert "session probe failed" in caplog.text


def test_the_logon_time_comes_from_its_seam():
    assert Win32Session(lambda: SessionState.UNLOCKED, lambda: _LOGON).logon_time() == _LOGON
    assert Win32Session(lambda: SessionState.UNLOCKED, lambda: None).logon_time() is None


def test_a_failing_logon_seam_is_logged_and_reads_none(caplog):
    with caplog.at_level(logging.WARNING, logger=backend.__name__):
        assert Win32Session(lambda: SessionState.UNLOCKED, _fail).logon_time() is None
    assert "logon time unreadable" in caplog.text


def test_no_session_probe_knows_nothing():
    probe = NoSessionProbe()
    assert probe.state() is None
    assert probe.logon_time() is None


@pytest.mark.parametrize("platform", ["linux", "darwin", "android"])
def test_a_platform_without_a_backend_gets_no_session_probe(platform):
    assert isinstance(session_probe(platform), NoSessionProbe)


@pytest.mark.parametrize("error", [ImportError("no ctypes.windll"), OSError("wtsapi32 missing")])
def test_a_backend_that_cannot_load_falls_back_to_no_session_probe(monkeypatch, caplog, error):
    def broken():
        raise error

    monkeypatch.setitem(backend._SESSION_REGISTRY, "win32", broken)
    with caplog.at_level(logging.WARNING, logger=backend.__name__):
        assert isinstance(session_probe("win32"), NoSessionProbe)
    assert "session probe backend for win32 unavailable" in caplog.text


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows backend loads wtsapi32")
def test_windows_gets_the_win32_session_probe_without_probing():
    assert isinstance(session_probe("win32"), Win32Session)
