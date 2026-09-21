"""Resolves where config, state, the ledger and diagnostic logs live.

The only module that knows these locations; everything else asks it.
Resolved through `platformdirs`. Never the repository, never a
cloud-synced path by default.
"""

from pathlib import Path

from platformdirs import user_config_dir, user_state_dir

_APP_NAME = "timeexisting"

CONFIG_FILENAME = "config.toml"
LOG_FILENAME = "tracker.log"


def config_dir() -> Path:
    return Path(user_config_dir(_APP_NAME, appauthor=False, roaming=True))


def config_file() -> Path:
    return config_dir() / CONFIG_FILENAME


def state_dir() -> Path:
    return Path(user_state_dir(_APP_NAME, appauthor=False))


def ledger_dir() -> Path:
    return state_dir() / "ledger"


def logs_dir() -> Path:
    return state_dir() / "logs"


def log_file() -> Path:
    return logs_dir() / LOG_FILENAME
