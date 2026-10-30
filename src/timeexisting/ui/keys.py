"""Non-blocking key polling for the viewer (roadmap section 12).

`key_source` yields a `poll` callable that returns one pending key per call,
or `None` when nothing is waiting. The render loop drains it once per tick,
between the sleep and the next repaint, so no thread ever blocks in a read:
a thread stuck in `getwch` or `read` cannot be told to stop, holds the
terminal's raw mode at exit, and is one more place a Ctrl+C can land.

Three cases, decided once at entry:

- The stream is not a terminal (tests, a pipe, `pythonw.exe` with no
  console): `poll` always answers `None` and nothing is imported or touched.
- Windows: `msvcrt.kbhit` says whether a key waits and `msvcrt.getwch`
  takes it. A `\\x00` or `\\xe0` prefix announces a special key (arrows,
  function keys) whose second half is consumed and the key dropped.
- POSIX: the terminal goes to cbreak for the duration (no echo, no line
  buffering, `ISIG` left on so Ctrl+C is still SIGINT), `select` with a zero
  timeout says whether a byte waits and `os.read` takes it. The saved
  attributes are restored with `TCSADRAIN` on every exit, exception included.

The platform modules are imported inside their branches, never at module
level: `cli.py` imports this package in the collector process too, which
has no console to speak of.
"""

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TextIO

Poll = Callable[[], str | None]

_SPECIAL_KEY_PREFIXES = ("\x00", "\xe0")


def _never() -> str | None:
    return None


def _is_terminal(stream: TextIO | None) -> bool:
    if stream is None:
        return False
    try:
        return stream.isatty()
    except AttributeError, ValueError:
        return False


def _windows_poll() -> Poll:
    import msvcrt

    def poll() -> str | None:
        if not msvcrt.kbhit():
            return None
        key = msvcrt.getwch()
        if key in _SPECIAL_KEY_PREFIXES:
            msvcrt.getwch()  # the scan code that follows the prefix
            return None
        return key

    return poll


@contextmanager
def _posix_poll(stream: TextIO) -> Iterator[Poll]:
    import os
    import select
    import termios
    import tty

    fd = stream.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:

        def poll() -> str | None:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                return None
            data = os.read(fd, 1)
            return data.decode(errors="replace") if data else None

        yield poll
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


@contextmanager
def key_source(stream: TextIO | None = None) -> Iterator[Poll]:
    """Yield a non-blocking `poll` over `stream`, `sys.stdin` when omitted (resolved at call time, so a redirected stdin is honoured). Not a terminal: a `poll` that always returns `None`, and nothing else happens."""
    if stream is None:
        stream = sys.stdin
    if not _is_terminal(stream):
        yield _never
        return
    if sys.platform == "win32":
        yield _windows_poll()
        return
    with _posix_poll(stream) as poll:
        yield poll
