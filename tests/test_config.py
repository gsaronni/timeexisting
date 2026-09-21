import re
from datetime import time, timedelta

import pytest

from timeexisting import paths
from timeexisting.config import loader
from timeexisting.config.models import ConfigError, parse_clock_time, parse_duration


def test_defaults_load():
    config = loader.load_config()

    assert config.version == 1
    assert config.contract.weekly_target == timedelta(hours=37)
    assert config.contract.daily_target == timedelta(hours=7, minutes=24)
    assert config.contract.working_days == ("mon", "tue", "wed", "thu", "fri")
    assert config.lunch.window_start == time(11, 0)
    assert config.lunch.deduction == timedelta(minutes=30)
    assert config.credit.default_start == time(9, 0)
    assert config.absence.meeting_slots == (timedelta(minutes=30), timedelta(hours=1))
    assert config.display.voice == "grimdark"
    assert config.profiles.hosts == {"EXAMPLE-HOST": "work"}
    assert config.power.action == "sleep"
    assert config.power.postpone_max == 1
    assert config.power.weekends is False


def test_parse_duration_rejects_malformed_text():
    with pytest.raises(ConfigError):
        parse_duration("not-a-duration")


def test_parse_duration_accepts_hours_minutes_and_seconds():
    assert parse_duration("1h30m") == timedelta(hours=1, minutes=30)
    assert parse_duration("45s") == timedelta(seconds=45)
    assert parse_duration("10m") == timedelta(minutes=10)


def test_parse_clock_time_rejects_a_string():
    with pytest.raises(ConfigError):
        parse_clock_time("11:00")


def test_unknown_top_level_key_fails(tmp_path, monkeypatch):
    overlay = tmp_path / "config.toml"
    overlay.write_text("[nonsense]\nkey = 1\n", encoding="utf-8")
    monkeypatch.setattr(paths, "config_file", lambda: overlay)

    with pytest.raises(ConfigError, match="unknown config key: nonsense"):
        loader.load_config()


def test_unknown_nested_key_fails(tmp_path, monkeypatch):
    overlay = tmp_path / "config.toml"
    overlay.write_text('[display]\nnonexistent = "x"\n', encoding="utf-8")
    monkeypatch.setattr(paths, "config_file", lambda: overlay)

    with pytest.raises(ConfigError, match=re.escape("unknown config key: display.nonexistent")):
        loader.load_config()


def test_malformed_duration_in_overlay_fails(tmp_path, monkeypatch):
    overlay = tmp_path / "config.toml"
    overlay.write_text('[contract]\nweekly_target = "not-a-duration"\n', encoding="utf-8")
    monkeypatch.setattr(paths, "config_file", lambda: overlay)

    with pytest.raises(ConfigError):
        loader.load_config()


def test_overlay_overrides_one_value_and_leaves_the_rest(tmp_path, monkeypatch):
    overlay = tmp_path / "config.toml"
    overlay.write_text('[display]\nvoice = "hygge"\n', encoding="utf-8")
    monkeypatch.setattr(paths, "config_file", lambda: overlay)

    config = loader.load_config()

    assert config.display.voice == "hygge"
    assert config.display.locale == "en"
    assert config.display.palette == "grimdark"
    assert config.contract.weekly_target == timedelta(hours=37)


def test_missing_overlay_file_yields_pure_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_file", lambda: tmp_path / "does-not-exist.toml")

    config = loader.load_config()

    assert config.display.voice == "grimdark"
    assert config.contract.weekly_target == timedelta(hours=37)
