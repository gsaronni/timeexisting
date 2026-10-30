"""`ui/keys.py`: the non-blocking key poll behind `q` (roadmap section 12, issue #3).

The POSIX cases run on a pseudo-terminal and skip on Windows; the Windows
cases fake `msvcrt` and skip elsewhere. The non-terminal case and the Ctrl+C
regression run everywhere.
"""

import sys
import time as time_module
from datetime import UTC, datetime, time
from io import StringIO

import pytest
from rich.console import Console

from timeexisting.domain.clock import FixedClock
from timeexisting.ui import app, keys

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="termios and openpty are POSIX only")
_WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="msvcrt is Windows only")


def _wait_for_key(poll, attempts: int = 50):
    """A byte written to the pty master reaches the slave through the line discipline; give it a moment."""
    for _ in range(attempts):
        key = poll()
        if key is not None:
            return key
        time_module.sleep(0.01)
    return None


@_POSIX_ONLY
def test_posix_poll_reads_one_key_then_nothing_and_restores_the_terminal():
    import os
    import termios

    master, slave = os.openpty()
    try:
        stream = os.fdopen(slave, "rb", buffering=0, closefd=False)
        original = termios.tcgetattr(slave)
        with keys.key_source(stream) as poll:
            assert poll() is None
            assert termios.tcgetattr(slave) != original  # cbreak is in force
            os.write(master, b"q")
            assert _wait_for_key(poll) == "q"
            assert poll() is None
        assert termios.tcgetattr(slave) == original
    finally:
        os.close(master)
        os.close(slave)


@_POSIX_ONLY
def test_posix_terminal_is_restored_after_an_exception_inside_the_block():
    import os
    import termios

    master, slave = os.openpty()
    try:
        stream = os.fdopen(slave, "rb", buffering=0, closefd=False)
        original = termios.tcgetattr(slave)
        with pytest.raises(RuntimeError), keys.key_source(stream):
            raise RuntimeError("the render loop blew up")
        assert termios.tcgetattr(slave) == original
    finally:
        os.close(master)
        os.close(slave)


class _Terminal(StringIO):
    """Claims to be a terminal, so the platform branch is taken."""

    def isatty(self) -> bool:
        return True


@_WINDOWS_ONLY
def test_windows_poll_returns_a_plain_key_and_none_when_nothing_waits(monkeypatch):
    import msvcrt

    pending = ["q"]
    monkeypatch.setattr(msvcrt, "kbhit", lambda: bool(pending))
    monkeypatch.setattr(msvcrt, "getwch", lambda: pending.pop(0))
    with keys.key_source(_Terminal()) as poll:
        assert poll() == "q"
        assert poll() is None


@_WINDOWS_ONLY
def test_windows_poll_swallows_a_special_key_as_one_pair(monkeypatch):
    import msvcrt

    pending = ["\xe0", "H", "q"]  # the up arrow, then a plain key
    monkeypatch.setattr(msvcrt, "kbhit", lambda: bool(pending))
    monkeypatch.setattr(msvcrt, "getwch", lambda: pending.pop(0))
    with keys.key_source(_Terminal()) as poll:
        assert poll() is None
        assert poll() == "q"
        assert poll() is None


class _NotATerminal(StringIO):
    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        pytest.fail("a stream that is not a terminal must not be touched")


class _MustNotBeCalled:
    """Stands in for the platform module: any attribute access is a test failure."""

    def __getattr__(self, name: str):
        pytest.fail(f"the platform module was used on a stream that is not a terminal: {name}")


def test_a_stream_that_is_not_a_terminal_polls_nothing_and_touches_nothing(monkeypatch):
    monkeypatch.setitem(sys.modules, "termios", _MustNotBeCalled())
    monkeypatch.setitem(sys.modules, "msvcrt", _MustNotBeCalled())
    with keys.key_source(_NotATerminal()) as poll:
        assert poll() is None
        assert poll() is None


def test_key_source_defaults_to_stdin_at_call_time(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _NotATerminal())
    with keys.key_source() as poll:
        assert poll() is None


def test_run_still_ends_on_ctrl_c_from_the_sleep_seam_with_keys_wired(cfg, monkeypatch):
    clock = FixedClock(datetime(2026, 10, 7, 9, 0, tzinfo=UTC))
    console = Console(file=StringIO(), force_terminal=True, width=80)
    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(app.time, "sleep", fake_sleep)
    with keys.key_source(_NotATerminal()) as poll:
        app.run(clock, cfg, time(8, 30), frozenset(), console=console, keys=poll)

    assert sleep_calls == [1.0]
    assert "liberated" in console.file.getvalue()
