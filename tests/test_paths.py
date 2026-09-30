import os
import subprocess
import sys

import pytest

from timeexisting import paths


def test_config_file_lives_under_config_dir():
    assert paths.config_file() == paths.config_dir() / "config.toml"


def test_ledger_and_logs_live_under_state_dir():
    assert paths.ledger_dir() == paths.state_dir() / "ledger"
    assert paths.log_dir() == paths.state_dir() / "logs"
    assert paths.log_file("collector").parent == paths.log_dir()


def test_per_machine_files_carry_the_host(monkeypatch):
    monkeypatch.setattr(paths.platform, "node", lambda: "TEST-HOST")
    assert paths.log_file("collector") == paths.log_dir() / "collector-TEST-HOST.log"
    assert paths.log_file("viewer") == paths.log_dir() / "viewer-TEST-HOST.log"
    assert paths.lock_file("collector") == paths.state_dir() / "collector-TEST-HOST.lock"
    assert paths.lock_file("viewer") == paths.state_dir() / "viewer-TEST-HOST.lock"
    assert paths.stop_file() == paths.state_dir() / "collector-TEST-HOST.stop"
    assert paths.checkpoint_file("TEST-HOST") == paths.state_dir() / "checkpoint-TEST-HOST.json"
    assert paths.checkpoint_file("odd/host").name == "checkpoint-odd_host.json"


def test_two_hosts_sharing_a_state_dir_never_share_a_file(monkeypatch):
    def files_for(host: str) -> set:
        monkeypatch.setattr(paths.platform, "node", lambda: host)
        return {paths.log_file("collector"), paths.lock_file("collector"), paths.stop_file()}

    assert files_for("laptop").isdisjoint(files_for("desktop"))


def test_per_machine_filenames_use_the_sanitised_host(monkeypatch):
    monkeypatch.setattr(paths.platform, "node", lambda: "odd/host")
    assert paths.lock_file("collector").name == "collector-odd_host.lock"


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("TEST-HOST", "TEST-HOST"),
        ("fedora.local", "fedora.local"),
        ("my box/with:odd*chars", "my_box_with_odd_chars"),
        ("..hidden", "hidden"),
        ("  ", "unknown-host"),
        ("", "unknown-host"),
        ("con", "con_"),
        ("NUL.home", "NUL.home_"),
        ("Hvidovre-Ø", "Hvidovre-_"),
    ],
)
def test_sanitise_host(host, expected):
    assert paths.sanitise_host(host) == expected


def test_current_host_comes_from_platform_node(monkeypatch):
    monkeypatch.setattr(paths.platform, "node", lambda: "odd/host")
    assert paths.current_host() == "odd_host"


def test_environment_overrides_both_roots(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, str(tmp_path / "synced" / "config"))
    monkeypatch.setenv(paths.STATE_DIR_ENV, str(tmp_path / "synced" / "state"))
    assert paths.config_dir() == tmp_path / "synced" / "config"
    assert paths.state_dir() == tmp_path / "synced" / "state"
    assert paths.ledger_dir() == tmp_path / "synced" / "state" / "ledger"


def test_blank_override_falls_back_to_platformdirs(monkeypatch):
    monkeypatch.setenv(paths.CONFIG_DIR_ENV, "  ")
    monkeypatch.setenv(paths.STATE_DIR_ENV, "")
    assert paths.config_dir().name == "timeexisting"
    assert paths.state_dir().name == "timeexisting"


def test_resolving_creates_nothing():
    paths.config_dir()
    paths.config_file()
    paths.state_dir()
    assert not paths.config_dir().exists()
    assert not paths.state_dir().exists()


def test_directories_are_created_on_first_use():
    assert not paths.state_dir().exists()
    assert paths.ledger_dir().is_dir()
    assert paths.log_dir().is_dir()
    assert paths.lock_file("collector").parent.is_dir()
    assert not paths.lock_file("collector").exists()
    assert not paths.stop_file().exists()


def test_importing_creates_nothing():
    # A fresh interpreter, inheriting the isolated environment, imports the module and nothing else.
    subprocess.run([sys.executable, "-c", "import timeexisting.paths"], check=True, env=os.environ.copy())
    assert not paths.config_dir().exists()
    assert not paths.state_dir().exists()


def test_config_dir_and_state_dir_are_distinct(monkeypatch):
    # This is specifically about the real platformdirs resolution (Windows collapses config and state to the same folder without appauthor=False, roaming=True), so it lifts the isolation for just this test.
    monkeypatch.delenv(paths.CONFIG_DIR_ENV)
    monkeypatch.delenv(paths.STATE_DIR_ENV)
    assert paths.config_dir() != paths.state_dir()
