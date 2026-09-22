from datetime import UTC, date, datetime, time

import pytest

from timeexisting import cli, paths
from timeexisting.domain.clock import FixedClock, SystemClock
from timeexisting.domain.schedule import DayFlag
from timeexisting.domain.segments import Phase


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


def test_demo_scenarios_cover_every_weekday_phase_and_weekend_day(cfg):
    reference = datetime(2026, 9, 17, 15, 0, tzinfo=UTC)  # a Thursday
    scenarios = cli._demo_scenarios(reference, cfg)
    labels = [label for label, _resolved in scenarios]

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

    expected_phases = [
        Phase.PRE_WORK,
        Phase.WORKING,
        Phase.LUNCH,
        Phase.WORKING,
        Phase.WORKING,  # final stretch: still working, five minutes before nominal_end
        Phase.POST_WORK,
        Phase.OFF_DAY,
        Phase.OFF_DAY,
    ]
    for (_label, resolved), expected_phase in zip(scenarios[:8], expected_phases, strict=True):
        assert resolved.segment.phase is expected_phase

    assert scenarios[6][1].now.weekday() == 5  # Saturday
    assert scenarios[7][1].now.weekday() == 6  # Sunday


def test_demo_scenarios_cover_every_month_on_a_weekday_at_noon(cfg):
    reference = datetime(2026, 9, 17, 15, 0, tzinfo=UTC)  # a Thursday
    scenarios = cli._demo_scenarios(reference, cfg)
    month_scenarios = scenarios[8:]

    assert len(month_scenarios) == 12
    months_seen = set()
    for label, resolved in month_scenarios:
        assert resolved.now.weekday() < 5
        assert resolved.now.hour == 12
        assert label != ""
        months_seen.add(resolved.now.month)
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


def test_config_check_prints_ok_and_the_path_on_success(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "config_file", lambda: tmp_path / "does-not-exist.toml")

    cli.main(["config", "check"])

    assert capsys.readouterr().out.strip() == f"ok {tmp_path / 'does-not-exist.toml'}"


def test_config_check_prints_the_error_to_stderr_and_exits_2_on_failure(capsys, tmp_path, monkeypatch):
    overlay = tmp_path / "config.toml"
    overlay.write_text('[display]\nnonexistent = "x"\n', encoding="utf-8")
    monkeypatch.setattr(paths, "config_file", lambda: overlay)

    with pytest.raises(SystemExit) as excinfo:
        cli.main(["config", "check"])

    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown config key: display.nonexistent" in captured.err


# --- Step 6: start-time resolution -----------------------------------------
#
# `--start`/`--flag` and the functions below are fully wired and tested here,
# but not yet called from main()'s viewer path: nothing renders from them
# until step 7 gives ui/panels/day.py a Resolved to render from. Exercising
# them directly (as here) is how they're verified in the meantime.


def test_start_flag_parses_hh_mm():
    args = cli.build_parser().parse_args(["--start", "08:30"])
    assert args.start == time(8, 30)


def test_start_flag_rejects_a_malformed_value():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--start", "not-a-time"])


def test_flag_option_accepts_every_documented_name_and_may_repeat():
    args = cli.build_parser().parse_args(
        ["--flag", "no-lunch", "--flag", "half-day", "--flag", "sick", "--flag", "offsite"]
    )
    assert args.flags == [DayFlag.NO_LUNCH, DayFlag.HALF_DAY, DayFlag.SICK, DayFlag.OFFSITE]


def test_flag_option_rejects_an_unknown_name():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--flag", "not-a-flag"])


def test_flags_default_to_an_empty_list():
    args = cli.build_parser().parse_args([])
    assert args.flags == []


def test_viewer_day_uses_at_when_given():
    args = cli.build_parser().parse_args(["--at", "2026-09-19 10:00"])  # a Saturday
    assert cli._viewer_day(args) == date(2026, 9, 19)


def test_prompt_start_returns_the_config_default_on_empty_input(cfg):
    start = cli._prompt_start(cfg, read=lambda _prompt: "", write=lambda _text: None)
    assert start == cfg.credit.default_start


def test_prompt_start_parses_a_given_time(cfg):
    start = cli._prompt_start(cfg, read=lambda _prompt: "08:30", write=lambda _text: None)
    assert start == time(8, 30)


def test_prompt_start_reprompts_on_a_malformed_value(cfg):
    responses = iter(["not-a-time", "08:30"])
    messages = []
    start = cli._prompt_start(cfg, read=lambda _prompt: next(responses), write=messages.append)
    assert start == time(8, 30)
    assert len(messages) == 1


def test_warn_outside_flex_band_fires_only_outside_the_band(cfg):
    warnings = []
    cli._warn_outside_flex_band(time(8, 30), cfg, write=warnings.append)
    assert warnings == []

    cli._warn_outside_flex_band(time(7, 0), cfg, write=warnings.append)
    assert len(warnings) == 1
    assert "flex band" in warnings[0]


def _unexpected_read(_prompt):
    raise AssertionError("should not prompt")


def test_resolve_start_uses_cli_start_without_prompting(cfg):
    args = cli.build_parser().parse_args(["--start", "08:30"])
    start = cli._resolve_start(args, cfg, date(2026, 9, 14), read=_unexpected_read)
    assert start == time(8, 30)


def test_resolve_start_skips_the_prompt_on_a_weekend(cfg):
    args = cli.build_parser().parse_args([])
    start = cli._resolve_start(args, cfg, date(2026, 9, 19), read=_unexpected_read)  # Saturday
    assert start == cfg.credit.default_start


def test_resolve_start_prompts_on_an_unflagged_weekday(cfg):
    args = cli.build_parser().parse_args([])
    start = cli._resolve_start(args, cfg, date(2026, 9, 14), read=lambda _p: "08:00", write=lambda _t: None)
    assert start == time(8, 0)
