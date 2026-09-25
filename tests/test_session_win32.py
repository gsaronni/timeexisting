import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

win32con = pytest.importorskip("win32con")
win32gui = pytest.importorskip("win32gui")

from timeexisting import paths  # noqa: E402
from timeexisting.collector import lockfile, session_win32  # noqa: E402
from timeexisting.collector.daemon import StopReason  # noqa: E402
from timeexisting.ledger import store  # noqa: E402


class _RecordingShutdown:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[StopReason] = []
        self._fail = fail

    def __call__(self, reason: StopReason) -> bool:
        self.calls.append(reason)
        if self._fail:
            raise OSError("disk gone")
        return len(self.calls) == 1


@pytest.fixture
def recording():
    return _RecordingShutdown()


@pytest.fixture
def watcher(recording):
    return session_win32.SessionWatcher(recording)


def test_query_end_session_says_yes_and_does_not_stop(watcher, recording):
    assert watcher.window_procedure(0, win32con.WM_QUERYENDSESSION, 0, 0) is True
    assert recording.calls == []


def test_end_session_cancelled_does_not_stop(watcher, recording):
    assert watcher.window_procedure(0, win32con.WM_ENDSESSION, 0, 0) == 0
    assert recording.calls == []


def test_end_session_stops_with_session_end(watcher, recording):
    assert watcher.window_procedure(0, win32con.WM_ENDSESSION, 1, 0) == 0
    assert recording.calls == [StopReason.SESSION_END]


def test_other_messages_go_to_the_default_procedure(watcher, recording, monkeypatch):
    forwarded = []
    monkeypatch.setattr(session_win32.win32gui, "DefWindowProc", lambda *args: forwarded.append(args) or 42)
    assert watcher.window_procedure(7, win32con.WM_TIMER, 1, 2) == 42
    assert forwarded == [(7, win32con.WM_TIMER, 1, 2)]
    assert recording.calls == []


@pytest.mark.parametrize(
    ("ctrl_type", "reason"),
    [
        (win32con.CTRL_LOGOFF_EVENT, StopReason.SESSION_END),
        (win32con.CTRL_SHUTDOWN_EVENT, StopReason.SESSION_END),
        (win32con.CTRL_CLOSE_EVENT, StopReason.CONSOLE_CLOSE),
    ],
)
def test_console_session_events_stop_and_are_handled(watcher, recording, ctrl_type, reason):
    assert watcher.console_handler(ctrl_type) is True
    assert recording.calls == [reason]


@pytest.mark.parametrize("ctrl_type", [win32con.CTRL_C_EVENT, win32con.CTRL_BREAK_EVENT])
def test_ctrl_c_and_break_are_left_to_the_signal_handlers(watcher, recording, ctrl_type):
    assert watcher.console_handler(ctrl_type) is False
    assert recording.calls == []


def test_both_paths_share_one_shutdown(watcher, recording):
    watcher.console_handler(win32con.CTRL_LOGOFF_EVENT)
    watcher.window_procedure(0, win32con.WM_ENDSESSION, 1, 0)
    assert recording.calls == [StopReason.SESSION_END, StopReason.SESSION_END]


def test_a_failing_shutdown_is_logged_not_raised(caplog):
    failing = session_win32.SessionWatcher(_RecordingShutdown(fail=True))
    assert failing.window_procedure(0, win32con.WM_ENDSESSION, 1, 0) == 0
    assert failing.console_handler(win32con.CTRL_SHUTDOWN_EVENT) is True
    assert "shutdown on session_end failed" in caplog.text


def test_the_real_window_is_message_only_and_receives_session_messages(watcher, recording):
    assert watcher.start()
    try:
        found = win32gui.FindWindowEx(
            win32con.HWND_MESSAGE, 0, session_win32.WINDOW_CLASS, session_win32.window_title(os.getpid())
        )
        assert found == watcher.hwnd
        assert win32gui.SendMessage(watcher.hwnd, win32con.WM_QUERYENDSESSION, 0, 0) == 1
        win32gui.SendMessage(watcher.hwnd, win32con.WM_ENDSESSION, 1, 0)
        assert recording.calls == [StopReason.SESSION_END]
    finally:
        watcher.close()
    assert not watcher._thread.is_alive()


def _wait_for(condition, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.1)
    pytest.fail("timed out")


def test_a_pythonw_collector_stops_cleanly_on_end_session():
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        pytest.skip("no pythonw.exe beside the interpreter")
    process = subprocess.Popen([str(pythonw), "-m", "timeexisting", "collect"])
    try:
        owner = _wait_for(lambda: lockfile.status("collector"))
        hwnd = _wait_for(
            lambda: win32gui.FindWindowEx(
                win32con.HWND_MESSAGE, 0, session_win32.WINDOW_CLASS, session_win32.window_title(owner.pid)
            )
        )

        assert win32gui.SendMessage(hwnd, win32con.WM_QUERYENDSESSION, 0, 0) == 1
        win32gui.SendMessage(hwnd, win32con.WM_ENDSESSION, 1, 0)

        # WM_ENDSESSION returns only after the stop is on disk: the process may die the moment it does.
        events = store.read_shard(store.shard_path(paths.current_host())).events
        assert [(str(e.event), e.data["reason"]) for e in events] == [
            ("collector_start", "launch"),
            ("collector_stop", "session_end"),
        ]
        assert not paths.checkpoint_file(paths.current_host()).exists()
        assert lockfile.status("collector") is None
        assert process.wait(timeout=15) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=15)
