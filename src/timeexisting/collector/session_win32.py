"""The collector's hidden window on Windows (roadmap section 3): clean stop on logoff and shutdown, and the live session and power notifications. Imported only on Windows.

The detached collector runs under `pythonw.exe` with no console, so a console control handler never fires there. A hidden top-level window on a daemon thread answers `WM_QUERYENDSESSION` with `True` and, on `WM_ENDSESSION` with `wParam` true, stops the collector with reason `session_end` before returning, since the process may be terminated as soon as it does. When `te collect` runs in a terminal, a console control handler is registered as well. Both paths call the same idempotent `Shutdown`, because logoff can deliver both.

The same window, and no second one, receives the session and power notifications. After creation it registers with `WTSRegisterSessionNotification` for this session only, and unregisters when it is destroyed. `WTS_SESSION_LOCK` and `WTS_SESSION_UNLOCK` become `lock` and `unlock`, source `wts`; `PBT_APMSUSPEND` becomes `suspend` and each resume message becomes `resume`, source `power`, with the message name in `detail`. The window thread never writes them: it puts them in the collector's `Inbox`, stamped at receipt, and the tick loop writes them (single writer). Every session-end, session-change and power message is logged at INFO with `wParam` and `lParam` on arrival, so a live test shows delivery even when a later step fails.

The window is top-level, not message-only. A message-only window (`HWND_MESSAGE` parent) cannot be enumerated and does not receive broadcast messages, and the session-end and power messages are delivered to the top-level windows of the session, so a message-only window never hears them: the first live restart proved it. The window is never shown (no `WS_VISIBLE`) and carries `WS_EX_TOOLWINDOW`, so it has no taskbar or Alt+Tab entry.
"""

import logging
import os
import threading

import pywintypes
import win32api
import win32con
import win32console
import win32gui
import win32ts

from timeexisting.collector.daemon import Inbox, Shutdown, StopReason
from timeexisting.ledger.events import EventType, Source

logger = logging.getLogger(__name__)

WINDOW_CLASS = "timeexisting-session-watcher"
READY_TIMEOUT = 5.0

# Never shown: no WS_VISIBLE. WS_POPUP avoids the default caption and frame of an overlapped window; WS_EX_TOOLWINDOW keeps it off the taskbar and out of Alt+Tab.
WINDOW_STYLE = win32con.WS_POPUP
WINDOW_EX_STYLE = win32con.WS_EX_TOOLWINDOW

# Session and power constants, from the Windows SDK 10.0.26100 headers, not from pywin32.
# WinUser.h: #define WM_WTSSESSION_CHANGE 0x02B1
WM_WTSSESSION_CHANGE = 0x02B1
# WinUser.h: #define WTS_SESSION_LOCK 0x7, #define WTS_SESSION_UNLOCK 0x8
WTS_SESSION_LOCK = 0x7
WTS_SESSION_UNLOCK = 0x8
# WinUser.h: #define WM_POWERBROADCAST 0x0218
WM_POWERBROADCAST = 0x0218
# WinUser.h: #define PBT_APMSUSPEND 0x0004, PBT_APMRESUMECRITICAL 0x0006, PBT_APMRESUMESUSPEND 0x0007, PBT_APMRESUMEAUTOMATIC 0x0012
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMECRITICAL = 0x0006
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
# WtsApi32.h: #define NOTIFY_FOR_THIS_SESSION 0
NOTIFY_FOR_THIS_SESSION = 0

_SESSION_EVENTS = {WTS_SESSION_LOCK: EventType.LOCK, WTS_SESSION_UNLOCK: EventType.UNLOCK}
_POWER_EVENTS = {
    PBT_APMSUSPEND: (EventType.SUSPEND, "PBT_APMSUSPEND"),
    PBT_APMRESUMECRITICAL: (EventType.RESUME, "PBT_APMRESUMECRITICAL"),
    PBT_APMRESUMESUSPEND: (EventType.RESUME, "PBT_APMRESUMESUSPEND"),
    PBT_APMRESUMEAUTOMATIC: (EventType.RESUME, "PBT_APMRESUMEAUTOMATIC"),
}

_CONSOLE_REASONS = {
    win32con.CTRL_CLOSE_EVENT: StopReason.CONSOLE_CLOSE,
    win32con.CTRL_LOGOFF_EVENT: StopReason.SESSION_END,
    win32con.CTRL_SHUTDOWN_EVENT: StopReason.SESSION_END,
}


def window_title(pid: int) -> str:
    """The watcher window's title. Carries the pid so a specific collector's window can be found without touching any other."""
    return f"timeexisting collector {pid}"


class SessionWatcher:
    def __init__(self, shutdown: Shutdown, inbox: Inbox) -> None:
        self._shutdown = shutdown
        self._inbox = inbox
        self.hwnd: int | None = None
        self.registered = False
        self.ready = threading.Event()
        self._thread: threading.Thread | None = None

    def _stop(self, reason: StopReason) -> None:
        try:
            self._shutdown(reason)
        except Exception:
            logger.exception("shutdown on %s failed", reason)

    def window_procedure(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == win32con.WM_QUERYENDSESSION:
            logger.info("WM_QUERYENDSESSION received: wParam %#x, lParam %#x", wparam, lparam)
            return True
        if message == win32con.WM_ENDSESSION:
            # Logged before the stop, so a live test shows delivery even if the write then fails.
            logger.info("WM_ENDSESSION received: wParam %#x, lParam %#x", wparam, lparam)
            if wparam:
                self._stop(StopReason.SESSION_END)
            return 0
        if message == WM_WTSSESSION_CHANGE:
            logger.info("WM_WTSSESSION_CHANGE received: wParam %#x, lParam %#x", wparam, lparam)
            event = _SESSION_EVENTS.get(wparam)
            if event is not None:
                self._inbox.put(event, Source.WTS)
            return 0
        if message == WM_POWERBROADCAST:
            logger.info("WM_POWERBROADCAST received: wParam %#x, lParam %#x", wparam, lparam)
            power = _POWER_EVENTS.get(wparam)
            if power is not None:
                event, name = power
                self._inbox.put(event, Source.POWER, name)
            return True
        if message == win32con.WM_DESTROY:
            self._unregister(hwnd)
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, message, wparam, lparam)

    def console_handler(self, ctrl_type: int) -> bool:
        reason = _CONSOLE_REASONS.get(ctrl_type)
        if reason is None:
            return False  # Ctrl+C and Ctrl+Break go on to the signal handlers.
        logger.info("console control event %d: %s", ctrl_type, reason)
        self._stop(reason)
        return True

    def _register(self, hwnd: int) -> None:
        """Ask for this session's change notifications. A failure is logged and the collector carries on: the probe still reconciles the session state each tick."""
        try:
            win32ts.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION)
        except pywintypes.error as error:
            logger.error(
                "WTSRegisterSessionNotification failed, no live lock and unlock this run: %s %s",
                error.winerror,
                error.strerror,
            )
            return
        self.registered = True
        logger.info("session notifications registered for window %#x", hwnd)

    def _unregister(self, hwnd: int) -> None:
        if not self.registered:
            return
        self.registered = False
        try:
            win32ts.WTSUnRegisterSessionNotification(hwnd)
        except pywintypes.error as error:
            logger.warning("WTSUnRegisterSessionNotification failed: %s %s", error.winerror, error.strerror)

    def _run_window(self) -> None:
        try:
            instance = win32api.GetModuleHandle(None)
            window_class = win32gui.WNDCLASS()
            window_class.hInstance = instance
            window_class.lpszClassName = WINDOW_CLASS
            window_class.lpfnWndProc = self.window_procedure
            atom = win32gui.RegisterClass(window_class)
            self.hwnd = win32gui.CreateWindowEx(
                WINDOW_EX_STYLE,
                atom,
                window_title(os.getpid()),
                WINDOW_STYLE,
                0,
                0,
                0,
                0,
                0,
                0,
                instance,
                None,
            )
        except Exception:
            logger.exception("could not create the session watcher window")
            self.ready.set()
            return
        logger.info("session watcher window %#x ready", self.hwnd)
        self._register(self.hwnd)
        self.ready.set()
        win32gui.PumpMessages()
        win32gui.UnregisterClass(WINDOW_CLASS, instance)

    def start(self) -> bool:
        """Start the window thread and wait for the window. Returns whether it exists."""
        self._thread = threading.Thread(target=self._run_window, name="session-watcher", daemon=True)
        self._thread.start()
        self.ready.wait(READY_TIMEOUT)
        return self.hwnd is not None

    def close(self, timeout: float = READY_TIMEOUT) -> None:
        """Destroy the window, which unregisters its session notifications, and end its thread. The collector never needs this, since the thread is a daemon; tests do."""
        if self.hwnd is not None:
            win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout)


def console_attached() -> bool:
    return bool(win32console.GetConsoleWindow())


def install(shutdown: Shutdown, inbox: Inbox) -> SessionWatcher:
    """Watch for session end on behalf of `shutdown` and put session and power transitions in `inbox`: a console control handler when a console is attached, and always the hidden top-level window. A window that cannot be created is logged and the collector carries on without it."""
    watcher = SessionWatcher(shutdown, inbox)
    if console_attached():
        win32api.SetConsoleCtrlHandler(watcher.console_handler, True)
        logger.info("console control handler registered")
    if not watcher.start():
        logger.error("no session watcher window: logoff will be recovered as an unclean stop")
    return watcher
