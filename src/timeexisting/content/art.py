"""ASCII art manifest and loader.

Filenames under assets/ascii_art/ are canonical; this module never renames
them. The manifest maps a semantic key to the real file, so a key can be
made to mean what its art actually shows without moving anything on disk.
"""

from functools import cache
from importlib.resources import files

ART_DIR = "assets/ascii_art"

MANIFEST: dict[str, str] = {
    "header": "header.txt",
    "coffee": "coffe.txt",
    "sunset": "sunset.txt",
    "lunch": "lunch.txt",
    "break_morning": "break_morning.txt",
    "break_afternoon": "break_afternoon.txt",
    "shutdown": "shutdown.txt",
    "gaming": "gaming.txt",
    "intimate": "intimate.txt",
    "reading": "reading.txt",
    "sleep": "sleep.txt",
    "presleep": "presleep.txt",
    "surf": "surf.txt",
    "mtb": "mtb.txt",
    "fencing": "fencing.txt",
    "chess": "chess.txt",
}


@cache
def load_art(key: str) -> str:
    path = files("timeexisting").joinpath(ART_DIR).joinpath(MANIFEST[key])
    return path.read_text(encoding="utf-8").rstrip()


def missing_art() -> list[str]:
    """Manifest keys whose backing file does not exist on disk.

    Called at startup, never at import time, so a broken manifest reports
    cleanly instead of failing inside the render loop.
    """
    art_dir = files("timeexisting").joinpath(ART_DIR)
    return [key for key, filename in MANIFEST.items() if not art_dir.joinpath(filename).is_file()]
