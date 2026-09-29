from datetime import UTC, date, datetime, time

import pytest

from timeexisting import cli, paths
from timeexisting.collector import lockfile, spawn
from timeexisting.content import phrases
from timeexisting.domain.clock import FixedClock, SystemClock
from timeexisting.domain.schedule import DayFlag


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


def test_viewer_now_uses_at_when_given():
    args = cli.build_parser().parse_args(["--at", "2026-09-19 10:00"])  # a Saturday
    now = cli._viewer_now(args)
    assert now.date() == date(2026, 9, 19)
    assert (now.hour, now.minute) == (10, 0)


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


def _at(hour: int, minute: int = 0, day: int = 14) -> datetime:
    # 2026-09-14 is a Monday; +5/+6 days are the weekend.
    return datetime(2026, 9, day, hour, minute, tzinfo=cli.local_timezone())


def test_resolve_start_uses_cli_start_without_prompting(cfg):
    args = cli.build_parser().parse_args(["--start", "08:30"])
    start = cli._resolve_start(args, cfg, _at(10, 0), read=_unexpected_read)
    assert start == time(8, 30)


def test_resolve_start_skips_the_prompt_on_a_weekend(cfg):
    args = cli.build_parser().parse_args([])
    start = cli._resolve_start(args, cfg, _at(10, 0, day=19), read=_unexpected_read)  # Saturday
    assert start == cfg.credit.default_start


def test_resolve_start_prompts_on_an_unflagged_weekday(cfg):
    args = cli.build_parser().parse_args([])
    start = cli._resolve_start(args, cfg, _at(10, 0), read=lambda _p: "08:00", write=lambda _t: None)
    assert start == time(8, 0)


def test_resolve_start_skips_the_prompt_in_the_night_window(cfg):
    args = cli.build_parser().parse_args([])
    assert cli._in_night_window(time(23, 0), cfg) is True
    assert cli._in_night_window(time(2, 0), cfg) is True
    assert cli._in_night_window(time(10, 0), cfg) is False

    start = cli._resolve_start(args, cfg, _at(23, 0), read=_unexpected_read)
    assert start == cfg.credit.default_start

    start = cli._resolve_start(args, cfg, _at(2, 0), read=_unexpected_read)
    assert start == cfg.credit.default_start


def test_resolve_start_skips_the_prompt_after_latest_credit(cfg):
    args = cli.build_parser().parse_args([])
    assert cfg.credit.latest == time(19, 0)

    start = cli._resolve_start(args, cfg, _at(20, 0), read=_unexpected_read)
    assert start == cfg.credit.default_start


def test_resolve_start_prompts_right_up_to_latest_credit(cfg):
    args = cli.build_parser().parse_args([])
    start = cli._resolve_start(args, cfg, _at(18, 59), read=lambda _p: "08:00", write=lambda _t: None)
    assert start == time(8, 0)


def test_collect_parses_with_and_without_a_subcommand():
    parser = cli.build_parser()
    assert parser.parse_args(["collect"]).collect_command is None
    assert parser.parse_args(["collect", "status"]).collect_command == "status"
    assert parser.parse_args(["collect", "stop"]).collect_command == "stop"


def test_collect_status_when_nothing_runs(capsys):
    cli.main(["collect", "status"])
    assert capsys.readouterr().out.strip() == phrases.pick("collect.status.not_running")


def test_collect_status_reports_the_live_owner(capsys):
    lock = lockfile.acquire("collector")
    cli.main(["collect", "status"])
    out = capsys.readouterr().out
    since = lock.status.started.astimezone(cli.local_timezone())
    assert str(lock.status.pid) in out
    assert lock.status.host in out
    assert f"{since:%Y-%m-%d %H:%M}" in out


def test_collect_refuses_when_a_collector_already_runs(capsys):
    lockfile.acquire("collector")
    with pytest.raises(SystemExit) as raised:
        cli.main(["collect"])
    assert raised.value.code == 1
    assert capsys.readouterr().err.strip() in _pool("instance.already_running")
    assert paths.log_file("collector").exists()


def test_collect_stop_when_nothing_runs(capsys, cfg):
    cli._collect_stop(cfg, sleep=lambda _: pytest.fail("must not wait"))
    assert capsys.readouterr().out.strip() == phrases.pick("collect.status.not_running")
    assert not paths.stop_file().exists()


def test_collect_stop_creates_the_stop_file_and_waits_for_the_lock(capsys, cfg):
    lock = lockfile.acquire("collector")
    polls: list[float] = []

    def sleep(seconds: float) -> None:
        polls.append(seconds)
        assert paths.stop_file().exists()
        if len(polls) == 3:
            lockfile.release(lock)

    cli._collect_stop(cfg, sleep=sleep)
    assert len(polls) == 3
    assert capsys.readouterr().out.strip() == phrases.pick("collect.stop.stopped")


def test_collect_stop_gives_up_after_two_poll_intervals(capsys, cfg):
    lockfile.acquire("collector")
    waited: list[float] = []
    with pytest.raises(SystemExit) as raised:
        cli._collect_stop(cfg, sleep=waited.append)
    assert raised.value.code == 1
    assert sum(waited) == pytest.approx(2 * cfg.collector.poll.total_seconds())
    assert capsys.readouterr().out.strip() == phrases.pick("collect.stop.timeout")


def _pool(key: str) -> set[str]:
    return set(phrases._pool(phrases.DEFAULT_VOICE, phrases.DEFAULT_LOCALE, key))


def test_no_collector_parses_and_defaults_off():
    parser = cli.build_parser()
    assert parser.parse_args([]).no_collector is False
    assert parser.parse_args(["--no-collector"]).no_collector is True


@pytest.mark.parametrize("argv", [["--no-collector"], ["--at", "2026-09-17 07:30"], ["--demo"]])
def test_start_collector_is_skipped_when_opted_out(argv, monkeypatch):
    monkeypatch.setattr(spawn, "ensure_running", lambda: pytest.fail("must not start a collector"))
    assert cli._start_collector(cli.build_parser().parse_args(argv)) is False


def test_start_collector_reports_a_spawn_that_never_took_the_lock(monkeypatch):
    monkeypatch.setattr(spawn, "ensure_running", lambda: None)
    assert cli._start_collector(cli.build_parser().parse_args([])) is True


def test_start_collector_reports_success_when_a_collector_runs(monkeypatch):
    lock = lockfile.acquire("collector")
    monkeypatch.setattr(spawn, "ensure_running", lambda: lock.status)
    assert cli._start_collector(cli.build_parser().parse_args([])) is False


def test_collector_since_follows_the_lock():
    assert cli._collector_since() is None
    lock = lockfile.acquire("collector")
    assert cli._collector_since() == lock.status.started
    lockfile.release(lock)
    assert cli._collector_since() is None


def _interrupt(_prompt: str) -> str:
    raise KeyboardInterrupt


def test_ctrl_c_at_the_start_prompt_exits_quietly_with_130(cfg):
    args = cli.build_parser().parse_args([])
    written: list[str] = []
    monday_morning = datetime(2026, 9, 14, 7, 30, tzinfo=cli.local_timezone())

    with pytest.raises(SystemExit) as raised:
        cli._start_or_exit(args, cfg, monday_morning, read=_interrupt, write=written.append)

    assert raised.value.code == 130
    assert raised.value.__suppress_context__
    assert written[-1] in _pool("start.interrupted")


def test_start_or_exit_passes_a_normal_answer_through(cfg):
    args = cli.build_parser().parse_args([])
    monday_morning = datetime(2026, 9, 14, 7, 30, tzinfo=cli.local_timezone())
    assert cli._start_or_exit(args, cfg, monday_morning, read=lambda _: "08:15", write=print) == time(8, 15)


def test_main_exits_130_without_a_traceback_on_ctrl_c_at_the_prompt(capsys, monkeypatch):
    monkeypatch.setattr(cli, "_prompt_start", lambda *_args, **_kwargs: _interrupt(""))
    monkeypatch.setattr(cli, "run", lambda *_args, **_kwargs: pytest.fail("must not reach the viewer"))

    with pytest.raises(SystemExit) as raised:
        cli.main(["--at", "2026-09-14 07:30"])

    assert raised.value.code == 130
    captured = capsys.readouterr()
    assert captured.out.strip() in _pool("start.interrupted")
    assert "Traceback" not in captured.err
