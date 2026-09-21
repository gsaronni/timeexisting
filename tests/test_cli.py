from datetime import UTC, datetime

import pytest

from timeexisting import cli, paths
from timeexisting.domain.clock import FixedClock, SystemClock


def test_no_arguments_yields_a_system_clock():
    args = cli.build_parser().parse_args([])
    clock, label = cli._build_clock(args)
    assert isinstance(clock, SystemClock)
    assert label == ""


def test_at_parses_as_europe_copenhagen_time():
    args = cli.build_parser().parse_args(["--at", "2026-09-17 07:30"])
    clock, label = cli._build_clock(args)
    assert isinstance(clock, FixedClock)
    assert label == ""

    local = clock.now().astimezone(cli.local_timezone())
    assert (local.hour, local.minute) == (7, 30)
    assert local.date() == datetime(2026, 9, 17, tzinfo=UTC).date()


def test_at_rejects_a_malformed_value():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--at", "not-a-date"])


def test_at_and_demo_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--at", "2026-09-17 07:30", "--demo"])


def test_demo_start_is_monday_of_the_reference_week():
    reference = datetime(2026, 9, 17, 15, 0, tzinfo=UTC)  # a Thursday
    start = cli._demo_start(reference)
    local_start = start.astimezone(cli.local_timezone())
    assert local_start.weekday() == 0
    assert local_start.date() < reference.astimezone(cli.local_timezone()).date()


def test_demo_scenarios_cover_every_weekday_phase_and_weekend_day():
    reference = datetime(2026, 9, 17, 15, 0, tzinfo=UTC)  # a Thursday
    scenarios = cli._demo_scenarios(reference)
    labels = [label for label, _moment in scenarios]

    assert labels[:8] == [
        "Pre-Work",
        "Morning Work",
        "Lunch",
        "Afternoon Work",
        "Final Stretch",
        "Post-Work",
        "Saturday",
        "Sunday",
    ]
    saturday_moment = scenarios[6][1].astimezone(cli.local_timezone())
    sunday_moment = scenarios[7][1].astimezone(cli.local_timezone())
    assert saturday_moment.weekday() == 5
    assert sunday_moment.weekday() == 6


def test_demo_scenarios_cover_every_month_on_a_weekday_at_noon():
    reference = datetime(2026, 9, 17, 15, 0, tzinfo=UTC)  # a Thursday
    scenarios = cli._demo_scenarios(reference)
    month_scenarios = scenarios[8:]

    assert len(month_scenarios) == 12
    months_seen = set()
    for label, moment in month_scenarios:
        local_moment = moment.astimezone(cli.local_timezone())
        assert local_moment.weekday() < 5
        assert local_moment.hour == 12
        assert label != ""
        months_seen.add(local_moment.month)
    assert months_seen == set(range(1, 13))


def test_demo_flag_parses_and_is_dispatched_outside_build_clock():
    args = cli.build_parser().parse_args(["--demo"])
    assert args.demo is True
    # --demo is handled by main() via run_demo/_demo_scenarios, not _build_clock.
    clock, label = cli._build_clock(args)
    assert isinstance(clock, SystemClock)
    assert label == ""


def test_config_path_prints_the_resolved_path(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_file", lambda: tmp_path / "config.toml")

    cli.main(["config", "path"])

    assert capsys.readouterr().out.strip() == str(tmp_path / "config.toml")


def test_config_init_writes_defaults_when_absent(capsys, tmp_path, monkeypatch):
    target = tmp_path / "nested" / "config.toml"
    monkeypatch.setattr(paths, "config_file", lambda: target)

    cli.main(["config", "init"])

    assert target.is_file()
    assert "weekly_target" in target.read_text(encoding="utf-8")
    assert "Wrote defaults" in capsys.readouterr().out


def test_config_init_does_not_overwrite_an_existing_file(capsys, tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    target.write_text("custom = true\n", encoding="utf-8")
    monkeypatch.setattr(paths, "config_file", lambda: target)

    cli.main(["config", "init"])

    assert target.read_text(encoding="utf-8") == "custom = true\n"
    assert "already exists" in capsys.readouterr().out
