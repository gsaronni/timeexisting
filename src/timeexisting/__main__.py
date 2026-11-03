"""Enables `python -m timeexisting`, which is what the viewer spawns via `pythonw.exe` and what the Startup shortcut runs.

On Windows a bare `collect` first runs the respawn hook (`collector/respawn_win32.py`, issue #10), before `cli` and everything it imports: a collector started inside a launcher's kill-on-close job starts the base interpreter outside it and exits here, so the five seconds the viewer gives a spawned collector to take its lock are not paid twice.
"""

import sys

from timeexisting.collector.respawn import wants_hook

if wants_hook(sys.platform, sys.argv[1:]):
    from timeexisting.collector.respawn_win32 import hook

    hook(sys.argv[1:])

from timeexisting.cli import main

if __name__ == "__main__":
    main()
