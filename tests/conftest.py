import logging

import pytest

from timeexisting import logging_setup, paths
from timeexisting.collector import spawn
from timeexisting.config.loader import load_config


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    """Cut every test off from the real config and state directories on the machine running them. Done through the environment overrides rather than by patching `paths`, so a subprocess started by a test inherits the isolation too.

    A test that specifically needs the real platformdirs resolution (see tests/test_paths.py) can delete the two variables with `monkeypatch.delenv`.
    """
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "config"))
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def _no_real_collector(monkeypatch):
    """No test starts a real detached collector. A test that exercises spawning passes its own `spawner` or `popen`."""

    def refuse(*_args, **_kwargs):
        pytest.fail("a test tried to spawn a real collector")

    monkeypatch.setattr(spawn, "spawn", refuse)


@pytest.fixture(autouse=True)
def _reset_logging():
    """Close any log handler a test installed, so the file under `tmp_path` is released and handlers never stack across tests."""
    root = logging.getLogger()
    level, raise_exceptions = root.level, logging.raiseExceptions
    yield
    logging_setup.reset()
    root.setLevel(level)
    logging.raiseExceptions = raise_exceptions


@pytest.fixture
def cfg():
    """The packaged defaults, isolated (via `_isolated_paths`) from whatever local config.toml happens to exist on this machine."""
    return load_config()
