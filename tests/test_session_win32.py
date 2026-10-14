import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

win32con = pytest.importorskip("win32con")
win32gui = pytest.importorskip("win32gui")
win32ts = pytest.importorskip("win32ts")
pywintypes = pytest.importorskip("pywintypes")

from timeexisting import paths  # noqa: E402
from timeexisting.collector import lockfile, session_win32  # noqa: E402
from timeexisting.collector.daemon import Inbox, StopReason, Transition  # noqa: E402
from timeexisting.domain.clock import FixedClock  # noqa: E402
from timeexisting.ledger import store  # noqa: E402
from timeexisting.ledger.events import EventType, Source  # noqa: E402

_T0 = datetime(2026, 9, 17, 6, 0, 0, tzinfo=UTC)


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


class _Registrations:
    """Stands in for `WTSRegisterSessionNotification` and its pair, so no test registers the real session."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int | None]] = []
        self.fail = False

    def register(self, hwnd: int, flags: int) -> None:
        self.calls.append(("register", hwnd, flags))
        if self.fail:
            raise pywintypes.error(1702, "WTSRegisterSessionNotification", "The binding handle is invalid.")

    def unregister(self, hwnd: int) -> None:
        self.calls.append(("unregister", hwnd, None))


@pytest.fixture(autouse=True)
def registrations(monkeypatch):
    fake = _Registrations()
    monkeypatch.setattr(session_win32.win32ts, "WTSRegisterSessionNotification", fake.register)
    monkeypatch.setattr(session_win32.win32ts, "WTSUnRegisterSessionNotification", fake.unregister)
    return fake


@pytest.fixture
def inbox():
    return Inbox(FixedClock(_T0))


@pytest.fixture
def watcher(recording, inbox):
    return session_win32.SessionWatcher(recording, inbox)


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


def test_a_failing_shutdown_is_logged_not_raised(caplog, inbox):
    failing = session_win32.SessionWatcher(_RecordingShutdown(fail=True), inbox)
    assert failing.window_procedure(0, win32con.WM_ENDSESSION, 1, 0) == 0
    assert failing.console_handler(win32con.CTRL_SHUTDOWN_EVENT) is True
    assert "shutdown on session_end failed" in caplog.text


def test_both_session_messages_are_logged_with_their_parameters(watcher, caplog):
    caplog.set_level("INFO", logger=session_win32.__name__)
    watcher.window_procedure(0, win32con.WM_QUERYENDSESSION, 0, 0x80000000)
    watcher.window_procedure(0, win32con.WM_ENDSESSION, 0, 0x1)
    assert "WM_QUERYENDSESSION received: wParam 0x0, lParam 0x80000000" in caplog.text
    assert "WM_ENDSESSION received: wParam 0x0, lParam 0x1" in caplog.text


def test_end_session_is_logged_before_a_failing_stop(caplog, inbox):
    caplog.set_level("INFO", logger=session_win32.__name__)
    failing = session_win32.SessionWatcher(_RecordingShutdown(fail=True), inbox)
    failing.window_procedure(0, win32con.WM_ENDSESSION, 1, 0)
    received = caplog.text.index("WM_ENDSESSION received: wParam 0x1, lParam 0x0")
    assert received < caplog.text.index("shutdown on session_end failed")


def _find_top_level(pid: int) -> int:
    return win32gui.FindWindow(session_win32.WINDOW_CLASS, session_win32.window_title(pid))


def test_the_real_window_is_hidden_top_level_and_receives_session_messages(watcher, recording):
    assert watcher.start()
    try:
        assert _find_top_level(os.getpid()) == watcher.hwnd
        assert (
            win32gui.FindWindowEx(
                win32con.HWND_MESSAGE, 0, session_win32.WINDOW_CLASS, session_win32.window_title(os.getpid())
            )
            == 0
        )
        assert win32gui.GetParent(watcher.hwnd) == 0
        assert not win32gui.IsWindowVisible(watcher.hwnd)
        assert win32gui.GetWindowLong(watcher.hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW
        assert not win32gui.GetWindowLong(watcher.hwnd, win32con.GWL_STYLE) & win32con.WS_VISIBLE
        assert win32gui.SendMessage(watcher.hwnd, win32con.WM_QUERYENDSESSION, 0, 0) == 1
        win32gui.SendMessage(watcher.hwnd, win32con.WM_ENDSESSION, 1, 0)
        assert recording.calls == [StopReason.SESSION_END]
    finally:
        watcher.close()
    assert not watcher._thread.is_alive()


class _ProbeWatcher(session_win32.SessionWatcher):
    """Records a private registered message, which no other window on the machine acts on."""

    def __init__(self, shutdown, inbox, probe: int) -> None:
        super().__init__(shutdown, inbox)
        self.probe = probe
        self.probed = threading.Event()

    def window_procedure(self, hwnd, message, wparam, lparam):
        if message == self.probe:
            self.probed.set()
            return 0
        return super().window_procedure(hwnd, message, wparam, lparam)


def test_the_real_window_receives_broadcast_messages(recording, inbox):
    """The property the message-only window lacked. A broadcast of a private message stands in for session end, which cannot be broadcast from a test without ending other applications."""
    probe = win32gui.RegisterWindowMessage(f"timeexisting-broadcast-probe-{os.getpid()}")
    watcher = _ProbeWatcher(recording, inbox, probe)
    assert watcher.start()
    try:
        win32gui.PostMessage(win32con.HWND_BROADCAST, probe, 0, 0)
        assert watcher.probed.wait(5.0)
    finally:
        watcher.close()
    assert recording.calls == []


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
        hwnd = _wait_for(lambda: _find_top_level(owner.pid))

        assert win32gui.SendMessage(hwnd, win32con.WM_QUERYENDSESSION, 0, 0) == 1
        win32gui.SendMessage(hwnd, win32con.WM_ENDSESSION, 1, 0)

        # WM_ENDSESSION returns only after the stop is on disk: the process may die the moment it does.
        # The startup probe reads this machine's session and may add an unlock; only the collector's own lines are pinned.
        events = store.read_shard(store.shard_path(paths.current_host())).events
        collector = [e for e in events if e.event in {EventType.COLLECTOR_START, EventType.COLLECTOR_STOP}]
        assert [(str(e.event), e.data["reason"]) for e in collector] == [
            ("collector_start", "launch"),
            ("collector_stop", "session_end"),
        ]
        assert {e.event for e in events} <= {
            EventType.COLLECTOR_START,
            EventType.COLLECTOR_STOP,
            EventType.UNLOCK,
        }
        assert not paths.checkpoint_file(paths.current_host()).exists()
        assert lockfile.status("collector") is None
        assert process.wait(timeout=15) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=15)


# Step 5: session and power notifications on the same window.


def test_session_and_power_constants_match_the_windows_sdk_headers():
    """Values as written in the Windows SDK 10.0.26100 WinUser.h and WtsApi32.h. pywin32 is a cross-check only where it defines the name."""
    assert session_win32.WM_WTSSESSION_CHANGE == 0x02B1
    assert (session_win32.WTS_SESSION_LOCK, session_win32.WTS_SESSION_UNLOCK) == (0x7, 0x8)
    assert session_win32.WM_POWERBROADCAST == 0x0218
    assert session_win32.PBT_APMSUSPEND == 0x0004
    assert session_win32.PBT_APMRESUMECRITICAL == 0x0006
    assert session_win32.PBT_APMRESUMESUSPEND == 0x0007
    assert session_win32.PBT_APMRESUMEAUTOMATIC == 0x0012
    assert session_win32.NOTIFY_FOR_THIS_SESSION == 0
    for name in (
        "WM_POWERBROADCAST",
        "PBT_APMSUSPEND",
        "PBT_APMRESUMECRITICAL",
        "PBT_APMRESUMESUSPEND",
        "PBT_APMRESUMEAUTOMATIC",
    ):
        if hasattr(win32con, name):
            assert getattr(win32con, name) == getattr(session_win32, name), name
    assert win32ts.NOTIFY_FOR_THIS_SESSION == session_win32.NOTIFY_FOR_THIS_SESSION


@pytest.mark.parametrize(
    ("code", "event"),
    [(0x7, EventType.LOCK), (0x8, EventType.UNLOCK)],
)
def test_session_lock_and_unlock_go_to_the_inbox_stamped_at_receipt(watcher, inbox, code, event):
    assert watcher.window_procedure(0, session_win32.WM_WTSSESSION_CHANGE, code, 1) == 0
    assert inbox.drain() == [Transition(_T0, event, Source.WTS)]


@pytest.mark.parametrize("code", [0x1, 0x2, 0x3, 0x4, 0x5, 0x6, 0x9, 0xA, 0xB])
def test_other_session_changes_are_logged_and_put_nothing(watcher, inbox, caplog, code):
    caplog.set_level("INFO", logger=session_win32.__name__)
    assert watcher.window_procedure(0, session_win32.WM_WTSSESSION_CHANGE, code, 1) == 0
    assert inbox.drain() == []
    assert f"WM_WTSSESSION_CHANGE received: wParam {code:#x}, lParam 0x1" in caplog.text


@pytest.mark.parametrize(
    ("code", "event", "name"),
    [
        (0x4, EventType.SUSPEND, "PBT_APMSUSPEND"),
        (0x6, EventType.RESUME, "PBT_APMRESUMECRITICAL"),
        (0x7, EventType.RESUME, "PBT_APMRESUMESUSPEND"),
        (0x12, EventType.RESUME, "PBT_APMRESUMEAUTOMATIC"),
    ],
)
def test_suspend_and_each_resume_message_go_to_the_inbox_with_the_message_name(
    watcher, inbox, code, event, name
):
    assert watcher.window_procedure(0, session_win32.WM_POWERBROADCAST, code, 0) is True
    assert inbox.drain() == [Transition(_T0, event, Source.POWER, name)]


@pytest.mark.parametrize("code", [0x0A, 0x8013])
def test_other_power_messages_are_logged_and_put_nothing(watcher, inbox, caplog, code):
    caplog.set_level("INFO", logger=session_win32.__name__)
    assert watcher.window_procedure(0, session_win32.WM_POWERBROADCAST, code, 0) is True
    assert inbox.drain() == []
    assert f"WM_POWERBROADCAST received: wParam {code:#x}, lParam 0x0" in caplog.text


def test_the_window_procedure_never_writes_the_ledger(watcher, inbox):
    watcher.window_procedure(0, session_win32.WM_WTSSESSION_CHANGE, 0x7, 1)
    watcher.window_procedure(0, session_win32.WM_POWERBROADCAST, 0x4, 0)
    watcher.window_procedure(0, session_win32.WM_POWERBROADCAST, 0x12, 0)
    watcher.window_procedure(0, session_win32.WM_WTSSESSION_CHANGE, 0x8, 1)
    assert store.read_shard(store.shard_path(paths.current_host())).events == ()
    assert [transition.event for transition in inbox.drain()] == [
        EventType.LOCK,
        EventType.SUSPEND,
        EventType.RESUME,
        EventType.UNLOCK,
    ]


def test_session_and_power_messages_never_stop_the_collector(watcher, recording):
    for code in (0x5, 0x6, 0x7, 0x8):
        watcher.window_procedure(0, session_win32.WM_WTSSESSION_CHANGE, code, 1)
    for code in (0x4, 0x7, 0x12):
        watcher.window_procedure(0, session_win32.WM_POWERBROADCAST, code, 0)
    assert recording.calls == []


def test_the_real_window_registers_for_this_session_and_unregisters_on_close(watcher, registrations):
    assert watcher.start()
    hwnd = watcher.hwnd
    assert watcher.registered
    assert registrations.calls == [("register", hwnd, 0)]
    watcher.close()
    assert not watcher._thread.is_alive()
    assert registrations.calls == [("register", hwnd, 0), ("unregister", hwnd, None)]


def test_a_failed_registration_is_logged_and_the_window_still_serves(
    watcher, registrations, recording, caplog
):
    registrations.fail = True
    assert watcher.start()
    try:
        assert not watcher.registered
        assert "WTSRegisterSessionNotification failed" in caplog.text
        win32gui.SendMessage(watcher.hwnd, win32con.WM_ENDSESSION, 1, 0)
        assert recording.calls == [StopReason.SESSION_END]
    finally:
        watcher.close()
    assert [call[0] for call in registrations.calls] == ["register"]


def test_the_real_window_puts_session_and_power_messages_in_the_inbox(watcher, inbox):
    assert watcher.start()
    try:
        assert win32gui.SendMessage(watcher.hwnd, session_win32.WM_WTSSESSION_CHANGE, 0x7, 1) == 0
        assert win32gui.SendMessage(watcher.hwnd, session_win32.WM_POWERBROADCAST, 0x4, 0) == 1
        assert win32gui.SendMessage(watcher.hwnd, session_win32.WM_POWERBROADCAST, 0x12, 0) == 1
        assert win32gui.SendMessage(watcher.hwnd, session_win32.WM_WTSSESSION_CHANGE, 0x8, 1) == 0
    finally:
        watcher.close()
    assert inbox.drain() == [
        Transition(_T0, EventType.LOCK, Source.WTS),
        Transition(_T0, EventType.SUSPEND, Source.POWER, "PBT_APMSUSPEND"),
        Transition(_T0, EventType.RESUME, Source.POWER, "PBT_APMRESUMEAUTOMATIC"),
        Transition(_T0, EventType.UNLOCK, Source.WTS),
    ]


def test_install_hands_the_inbox_to_the_window(recording, inbox, monkeypatch):
    monkeypatch.setattr(session_win32, "console_attached", lambda: False)
    watcher = session_win32.install(recording, inbox)
    try:
        win32gui.SendMessage(watcher.hwnd, session_win32.WM_WTSSESSION_CHANGE, 0x7, 1)
    finally:
        watcher.close()
    assert inbox.drain() == [Transition(_T0, EventType.LOCK, Source.WTS)]
