"""Voice-pack loader.

Every user-visible string in the program is a pool entry read through this
module, keyed by a dotted path into content/voices/<voice>/<locale>.toml.
Resolution falls back voice+locale, then voice+en, then grimdark+en, and
never raises on a missing key or a missing pack: an unresolved key renders
as an empty string. Selection within a pool is a shuffled deque, refilled
on exhaustion, so a pool is never drained in place and never repeats an
entry within one cycle.
"""

import random
import tomllib
from collections import deque
from functools import cache
from importlib.resources import files

DEFAULT_VOICE = "grimdark"
DEFAULT_LOCALE = "en"

_VOICES_DIR = "content/voices"

_decks: dict[tuple[str, str, str], deque[str]] = {}


@cache
def _load_pack(voice: str, locale: str) -> dict:
    path = files("timeexisting").joinpath(_VOICES_DIR).joinpath(voice).joinpath(f"{locale}.toml")
    if not path.is_file():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _fallback_chain(voice: str, locale: str) -> tuple[tuple[str, str], ...]:
    candidates = [(voice, locale), (voice, DEFAULT_LOCALE), (DEFAULT_VOICE, DEFAULT_LOCALE)]
    seen: set[tuple[str, str]] = set()
    chain = []
    for pair in candidates:
        if pair not in seen:
            seen.add(pair)
            chain.append(pair)
    return tuple(chain)


def _lookup(pack: dict, key: str) -> list | None:
    node = pack
    for part in key.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
        if node is None:
            return None
    return node if isinstance(node, list) else None


def _pool(voice: str, locale: str, key: str) -> list[str]:
    for pack_voice, pack_locale in _fallback_chain(voice, locale):
        pool = _lookup(_load_pack(pack_voice, pack_locale), key)
        if pool:
            return pool
    return []


def pick(
    key: str,
    *,
    voice: str = DEFAULT_VOICE,
    locale: str = DEFAULT_LOCALE,
    rng: random.Random | None = None,
) -> str:
    pool = _pool(voice, locale, key)
    if not pool:
        return ""

    deck_key = (voice, locale, key)
    deck = _decks.get(deck_key)
    if not deck:
        shuffled = list(pool)
        (rng or random).shuffle(shuffled)
        deck = deque(shuffled)
        _decks[deck_key] = deck
    return deck.popleft()
