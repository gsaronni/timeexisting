import pytest

from timeexisting import paths
from timeexisting.config.loader import load_config


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    """Cut every test off from the real config and state directories on the
    machine running them. `config_file`, `ledger_dir`, `logs_dir` and
    `log_file` all derive from `config_dir`/`state_dir` internally, so
    patching just these two is enough to redirect all of them.

    A test that specifically needs the real platformdirs resolution (see
    tests/test_paths.py) can call `monkeypatch.undo()` to lift this.
    """
    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr(paths, "state_dir", lambda: tmp_path / "state")


@pytest.fixture
def cfg():
    """The packaged defaults, isolated (via `_isolated_paths`) from
    whatever local config.toml happens to exist on this machine.
    """
    return load_config()
