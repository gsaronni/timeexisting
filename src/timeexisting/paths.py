"""Resolves where config, state, the ledger and diagnostic logs live, and what this machine's files are called.

The only module that knows these locations; everything else asks it. Resolved through `platformdirs`, never the repository, never a cloud-synced path by default. `TIMEEXISTING_CONFIG_DIR` and `TIMEEXISTING_STATE_DIR` override the two roots when set, for anyone who wants one config and one ledger synced across machines by their own means.

Every per-machine file in the state directory carries the host in its name (`<role>-<host>.lock`, `collector-<host>.stop`, `checkpoint-<host>.json`, `logs/<role>-<host>.log`, like the ledger shards), so machines sharing a synced state directory never contend for the same file.

Nothing is created at import. The state-side accessors that hand out a location to write into (`ledger_dir`, `log_dir`, `lock_file`, `stop_file`, `checkpoint_file`) create their directory on first use; `config_dir` and `state_dir` only resolve.
"""

import os
import platform
import re
from pathlib import Path

from platformdirs import user_config_dir, user_state_dir

_APP_NAME = "timeexisting"

CONFIG_DIR_ENV = "TIMEEXISTING_CONFIG_DIR"
STATE_DIR_ENV = "TIMEEXISTING_STATE_DIR"

CONFIG_FILENAME = "config.toml"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"} | {f"COM{n}" for n in range(1, 10)} | {f"LPT{n}" for n in range(1, 10)}
)
_FALLBACK_HOST = "unknown-host"


def sanitise_host(host: str) -> str:
    """A hostname reduced to a safe, portable filename stem: anything outside letters, digits, `.`, `_` and `-` becomes `_`, leading dots are dropped, and Windows device names are suffixed."""
    safe = _UNSAFE.sub("_", host.strip()).lstrip(".")
    if not safe:
        return _FALLBACK_HOST
    if safe.split(".")[0].upper() in _WINDOWS_RESERVED:
        safe = f"{safe}_"
    return safe


def current_host() -> str:
    """This machine's name as it appears in filenames and in every event's `host` field, from `platform.node()`."""
    return sanitise_host(platform.node())


def _override(variable: str) -> Path | None:
    value = os.environ.get(variable, "").strip()
    return Path(value) if value else None


def _ensure(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def config_dir() -> Path:
    return _override(CONFIG_DIR_ENV) or Path(user_config_dir(_APP_NAME, appauthor=False, roaming=True))


def config_file() -> Path:
    return config_dir() / CONFIG_FILENAME


def state_dir() -> Path:
    return _override(STATE_DIR_ENV) or Path(user_state_dir(_APP_NAME, appauthor=False))


def ledger_dir() -> Path:
    return _ensure(state_dir() / "ledger")


def log_dir() -> Path:
    return _ensure(state_dir() / "logs")


def log_file(role: str) -> Path:
    return log_dir() / f"{role}-{current_host()}.log"


def lock_file(role: str) -> Path:
    return _ensure(state_dir()) / f"{role}-{current_host()}.lock"


def stop_file() -> Path:
    return _ensure(state_dir()) / f"collector-{current_host()}.stop"


def checkpoint_file(host: str) -> Path:
    """The collector's per-tick liveness file for `host`. Takes the host explicitly, unlike the others, because recovery reads the checkpoint of the host whose shard it inspects."""
    return _ensure(state_dir()) / f"checkpoint-{sanitise_host(host)}.json"
