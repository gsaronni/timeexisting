"""Diagnostic logging: one rotating file per role, `collector.log` and `viewer.log`.

One file per role because the collector and the viewer run at the same time, and Windows cannot rename a file another process holds open: with a shared file every rollover would fail and records would be dropped until the other process let go.

Never stdout, never stderr. The viewer owns the terminal through a Rich `Live` display, which corrupts on interleaved writes, and the detached collector has no terminal at all. `logging.raiseExceptions` is switched off for the same reason: a failed emit must drop the record, not print a traceback to stderr.
"""

import logging
from logging.handlers import RotatingFileHandler

from timeexisting import paths

MAX_BYTES = 1_000_000
BACKUP_COUNT = 3
FORMAT = "%(asctime)s %(role)s %(levelname)s %(name)s: %(message)s"

_handler: RotatingFileHandler | None = None


def configure(role: str) -> None:
    """Install the file handler for `role` on the root logger, tagging every record with it. Calling it again replaces the previous handler rather than stacking a second one."""
    global _handler
    reset()
    handler = RotatingFileHandler(
        paths.log_file(role), maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(FORMAT, defaults={"role": role}))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.raiseExceptions = False
    _handler = handler


def reset() -> None:
    """Remove and close the handler `configure` installed, if any."""
    global _handler
    if _handler is None:
        return
    logging.getLogger().removeHandler(_handler)
    _handler.close()
    _handler = None
