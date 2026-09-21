import pytest

from timeexisting import paths
from timeexisting.config.loader import load_config


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """The packaged defaults, isolated from whatever local config.toml
    happens to exist on the machine running the tests.
    """
    monkeypatch.setattr(paths, "config_file", lambda: tmp_path / "does-not-exist.toml")
    return load_config()
