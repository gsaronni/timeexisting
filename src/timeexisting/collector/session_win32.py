"""Clean collector stop on Windows logoff and shutdown (roadmap section 3). Imported only on Windows.

The detached collector runs under `pythonw.exe` with no console, so a console control handler never fires there. A hidden message-only window on a daemon thread answers `WM_QUERYENDSESSION` with `True` and, on `WM_ENDSESSION` with `wParam` true, stops the collector with reason `session_end` before returning, since the process may be terminated as soon as it does. When `te collect` runs in a terminal, a console control handler is registered as well. Both paths call the same idempotent `Shutdown`, because logoff can deliver both.
"""

import logging
import os
import threading

import win32api
import win32con
import win32console
import win32gui

from timeexisting.collector.daemon import Shutdown, StopReason

logger = logging.getLogger(__name__)

WINDOW_CLASS = "timeexisting-session-watcher"
READY_TIMEOUT = 5.0

_CONSOLE_REASONS = {
    win32con.CTRL_CLOSE_EVENT: StopReason.CONSOLE_CLOSE,
    win32con.CTRL_LOGOFF_EVENT: StopReason.SESSION_END,
    win32con.CTRL_SHUTDOWN_EVENT: StopReason.SESSION_END,
}


def window_title(pid: int) -> str:
    """The watcher window's title. Carries the pid so a specific collector's window can be found without touching any other."""
    return f"timeexisting collector {pid}"


class SessionWatcher:
    def __init__(self, shutdown: Shutdown) -> None:
        self._shutdown = shutdown
        self.hwnd: int | None = None
        self.ready = threading.Event()
        self._thread: threading.Thread | None = None

    def _stop(self, reason: StopReason) -> None:
        try:
            self._shutdown(reason)
        except Exception:
            logger.exception("shutdown on %s failed", reason)

    def window_procedure(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == win32con.WM_QUERYENDSESSION:
            return True
        if message == win32con.WM_ENDSESSION:
            if wparam:
                logger.info("WM_ENDSESSION: session ending")
                self._stop(StopReason.SESSION_END)
            return 0
        if message == win32con.WM_DESTROY:
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

    def _run_window(self) -> None:
        try:
            instance = win32api.GetModuleHandle(None)
            window_class = win32gui.WNDCLASS()
            window_class.hInstance = instance
            window_class.lpszClassName = WINDOW_CLASS
            window_class.lpfnWndProc = self.window_procedure
            atom = win32gui.RegisterClass(window_class)
            self.hwnd = win32gui.CreateWindowEx(
                0, atom, window_title(os.getpid()), 0, 0, 0, 0, 0, win32con.HWND_MESSAGE, 0, instance, None
            )
        except Exception:
            logger.exception("could not create the session watcher window")
            self.ready.set()
            return
        logger.info("session watcher window %#x ready", self.hwnd)
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
        """Destroy the window and end its thread. The collector never needs this, since the thread is a daemon; tests do."""
        if self.hwnd is not None:
            win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout)


def console_attached() -> bool:
    return bool(win32console.GetConsoleWindow())


def install(shutdown: Shutdown) -> SessionWatcher:
    """Watch for session end on behalf of `shutdown`: a console control handler when a console is attached, and always the message-only window. A window that cannot be created is logged and the collector carries on without it."""
    watcher = SessionWatcher(shutdown)
    if console_attached():
        win32api.SetConsoleCtrlHandler(watcher.console_handler, True)
        logger.info("console control handler registered")
    if not watcher.start():
        logger.error("no session watcher window: logoff will be recovered as an unclean stop")
    return watcher
