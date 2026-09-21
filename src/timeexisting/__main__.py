"""Enables `python -m timeexisting`, which is what the collector spawns via
`pythonw.exe` starting from phase 2.
"""

from timeexisting.cli import main

if __name__ == "__main__":
    main()
