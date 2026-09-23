"""Resolves where config, state, the ledger and diagnostic logs live.

The only module that knows these locations; everything else asks it. Resolved through `platformdirs`, never the repository, never a cloud-synced path by default. `TIMEEXISTING_CONFIG_DIR` and `TIMEEXISTING_STATE_DIR` override the two roots when set, for anyone who wants one config and one ledger synced across machines by their own means.

Nothing is created at import. The state-side accessors that hand out a location to write into (`ledger_dir`, `log_dir`, `lock_file`, `stop_file`) create their directory on first use; `config_dir` and `state_dir` only resolve.
"""

import os
from pathlib import Path

from platformdirs import user_config_dir, user_state_dir

_APP_NAME = "timeexisting"

CONFIG_DIR_ENV = "TIMEEXISTING_CONFIG_DIR"
STATE_DIR_ENV = "TIMEEXISTING_STATE_DIR"

CONFIG_FILENAME = "config.toml"
STOP_FILENAME = "collector.stop"


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
    return log_dir() / f"{role}.log"


def lock_file(role: str) -> Path:
    return _ensure(state_dir()) / f"{role}.lock"


def stop_file() -> Path:
    return _ensure(state_dir()) / STOP_FILENAME
