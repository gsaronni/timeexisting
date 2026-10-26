from collections.abc import Callable
from datetime import UTC, date, datetime, time

import pytest

from timeexisting import cli, paths
from timeexisting.collector import daemon, lockfile, spawn
from timeexisting.content import phrases
from timeexisting.domain.clock import FixedClock, SystemClock
from timeexisting.domain.day_start import DayStart, Provenance
from timeexisting.domain.schedule import DayFlag
from timeexisting.ledger import store
from timeexisting.ledger.events import Confidence, Event, EventType, Source


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

    cli._warn_outside_flex_band(time(9, 15), cfg, write=warnings.append)
    assert len(warnings) == 1
    assert warnings[0] in {
        line.format(start="09:15", flex_start="08:00", flex_end="09:00")
        for line in _pool("start.outside_flex")
    }


def _unexpected_read(_prompt):
    raise AssertionError("should not prompt")


def _at(hour: int, minute: int = 0, day: int = 14) -> datetime:
    # 2026-09-14 is a Monday; +5/+6 days are the weekend.
    return datetime(2026, 9, day, hour, minute, tzinfo=cli.local_timezone())


_NO_LEDGER_START = lambda _now: None  # noqa: E731


def _recorder() -> tuple[list[tuple[time, str]], Callable[[time, str], None]]:
    recorded: list[tuple[time, str]] = []
    return recorded, lambda start, detail: recorded.append((start, detail))


def test_resolve_start_uses_cli_start_without_prompting_and_records_it(cfg):
    args = cli.build_parser().parse_args(["--start", "08:30"])
    recorded, record = _recorder()
    start = cli._resolve_start(
        args, cfg, _at(10, 0), read=_unexpected_read, suggest=_NO_LEDGER_START, record=record
    )
    assert start == DayStart(time(8, 30), Provenance.FLAG)
    assert recorded == [(time(8, 30), "flag")]


def test_resolve_start_skips_the_prompt_on_a_weekend(cfg):
    args = cli.build_parser().parse_args([])
    recorded, record = _recorder()
    start = cli._resolve_start(
        args, cfg, _at(10, 0, day=19), read=_unexpected_read, record=record
    )  # Saturday
    assert start == DayStart(cfg.credit.default_start, Provenance.INFERRED)
    assert recorded == []


def test_resolve_start_prompts_on_an_unflagged_weekday_and_records_the_answer(cfg):
    args = cli.build_parser().parse_args([])
    recorded, record = _recorder()
    start = cli._resolve_start(
        args,
        cfg,
        _at(10, 0),
        read=lambda _p: "08:00",
        write=lambda _t: None,
        suggest=_NO_LEDGER_START,
        record=record,
    )
    assert start == DayStart(time(8, 0), Provenance.PROMPT)
    assert recorded == [(time(8, 0), "prompt")]


def test_resolve_start_skips_the_prompt_in_the_night_window(cfg):
    args = cli.build_parser().parse_args([])
    assert cli._in_night_window(time(23, 0), cfg) is True
    assert cli._in_night_window(time(2, 0), cfg) is True
    assert cli._in_night_window(time(10, 0), cfg) is False
    recorded, record = _recorder()

    start = cli._resolve_start(
        args, cfg, _at(23, 0), read=_unexpected_read, suggest=_NO_LEDGER_START, record=record
    )
    assert start == DayStart(cfg.credit.default_start, Provenance.INFERRED)

    start = cli._resolve_start(
        args, cfg, _at(2, 0), read=_unexpected_read, suggest=_NO_LEDGER_START, record=record
    )
    assert start == DayStart(cfg.credit.default_start, Provenance.INFERRED)
    assert recorded == []


def test_resolve_start_skips_the_prompt_after_latest_credit(cfg):
    args = cli.build_parser().parse_args([])
    assert cfg.credit.latest == time(19, 0)
    recorded, record = _recorder()

    start = cli._resolve_start(
        args, cfg, _at(20, 0), read=_unexpected_read, suggest=_NO_LEDGER_START, record=record
    )
    assert start == DayStart(cfg.credit.default_start, Provenance.INFERRED)
    assert recorded == []


def test_resolve_start_prompts_right_up_to_latest_credit(cfg):
    args = cli.build_parser().parse_args([])
    start = cli._resolve_start(
        args,
        cfg,
        _at(18, 59),
        read=lambda _p: "08:00",
        write=lambda _t: None,
        suggest=_NO_LEDGER_START,
        record=_discard,
    )
    assert start == DayStart(time(8, 0), Provenance.PROMPT)


def _discard(_start: time, _detail: str) -> None:
    pass


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


def test_start_or_exit_passes_a_normal_answer_through(cfg, monkeypatch):
    monkeypatch.setattr(daemon, "system_boot_time", lambda: None)
    args = cli.build_parser().parse_args([])
    monday_morning = datetime(2026, 9, 14, 7, 30, tzinfo=cli.local_timezone())
    start = cli._start_or_exit(args, cfg, monday_morning, read=lambda _: "08:15", write=print)
    assert start == DayStart(time(8, 15), Provenance.PROMPT)


def test_main_exits_130_without_a_traceback_on_ctrl_c_at_the_prompt(capsys, monkeypatch):
    monkeypatch.setattr(cli, "_prompt_start", lambda *_args, **_kwargs: _interrupt(""))
    monkeypatch.setattr(cli, "run", lambda *_args, **_kwargs: pytest.fail("must not reach the viewer"))

    with pytest.raises(SystemExit) as raised:
        cli.main(["--at", "2026-09-14 07:30"])

    assert raised.value.code == 130
    captured = capsys.readouterr()
    assert captured.out.strip() in _pool("start.interrupted")
    assert "Traceback" not in captured.err


def _ledger_event(ts: datetime, kind: str, source: Source = Source.COLLECTOR, **data: str) -> Event:
    return Event.new(
        ts=ts,
        host=paths.current_host(),
        profile="work",
        event=EventType(kind),
        source=source,
        confidence=Confidence.INFERRED if source is Source.PROBE else Confidence.OBSERVED,
        data=data,
    )


def _utc(day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, second, tzinfo=UTC)


def _no_boot() -> datetime | None:
    return None


_TUESDAY_10_LOCAL = datetime(2026, 9, 29, 10, 0, tzinfo=cli.local_timezone())


def test_ledger_start_reads_this_hosts_shard(cfg):
    store.append(_ledger_event(_utc(29, 6, 12, 30), "collector_start", reason="launch"))
    store.append(_ledger_event(_utc(29, 6, 12, 30), "unlock", Source.EVENTLOG))  # 08:12:30 local
    assert cli._ledger_start(_TUESDAY_10_LOCAL, cfg, boot_time=_no_boot) == DayStart(
        time(8, 12), Provenance.OBSERVED
    )


def test_ledger_start_is_none_without_a_ledger(cfg):
    assert cli._ledger_start(_TUESDAY_10_LOCAL, cfg, boot_time=_no_boot) is None


def test_ledger_start_survives_an_unreadable_ledger(cfg, monkeypatch):
    def unreadable(_path):
        raise PermissionError("locked")

    monkeypatch.setattr(store, "read_shard", unreadable)
    assert cli._ledger_start(_TUESDAY_10_LOCAL, cfg, boot_time=_no_boot) is None


def test_ledger_start_clamps_on_the_machines_boot_time(cfg):
    """Rule 4 through the seam: presence open since yesterday, no boot in the ledger, the collector down."""
    store.append(_ledger_event(_utc(28, 14, 0), "collector_start", reason="launch"))
    store.append(_ledger_event(_utc(28, 14, 0), "unlock", Source.PROBE))
    booted_today = _utc(29, 7, 0)  # 09:00 local
    assert cli._ledger_start(_TUESDAY_10_LOCAL, cfg, boot_time=lambda: booted_today) is None
    booted_yesterday = _utc(28, 10, 0)
    expected = DayStart(cfg.credit.earliest, Provenance.INFERRED)
    assert cli._ledger_start(_TUESDAY_10_LOCAL, cfg, boot_time=lambda: booted_yesterday) == expected
    assert cli._ledger_start(_TUESDAY_10_LOCAL, cfg, boot_time=_no_boot) == expected


def test_ledger_start_reads_the_boot_time_from_the_collectors_seam_by_default(cfg, monkeypatch):
    store.append(_ledger_event(_utc(28, 14, 0), "collector_start", reason="launch"))
    store.append(_ledger_event(_utc(28, 14, 0), "unlock", Source.PROBE))
    monkeypatch.setattr(daemon, "system_boot_time", lambda: _utc(29, 7, 0))
    assert cli._ledger_start(_TUESDAY_10_LOCAL, cfg) is None
    monkeypatch.setattr(daemon, "system_boot_time", lambda: None)
    assert cli._ledger_start(_TUESDAY_10_LOCAL, cfg) == DayStart(cfg.credit.earliest, Provenance.INFERRED)


def test_ledger_start_replays_with_the_configured_micro_max(cfg, monkeypatch):
    seen: list[object] = []
    real_replay = cli.replay

    def spy(events, **kwargs):
        seen.append(kwargs.get("micro_max"))
        return real_replay(events, **kwargs)

    monkeypatch.setattr(cli, "replay", spy)
    cli._ledger_start(_TUESDAY_10_LOCAL, cfg, boot_time=_no_boot)
    assert seen == [cfg.absence.micro_max]


def test_record_start_writes_a_day_start_for_the_local_day(cfg):
    cli._record_start(time(8, 55), "prompt", _TUESDAY_10_LOCAL, cfg)
    [event] = store.read_shard(store.shard_path(paths.current_host())).events
    assert (event.event, event.source, event.confidence) == (
        EventType.DAY_START,
        Source.USER,
        Confidence.OBSERVED,
    )
    assert event.ts == _TUESDAY_10_LOCAL
    assert event.host == paths.current_host()
    assert event.profile == cfg.profiles.default
    assert dict(event.data) == {"start": "2026-09-29T06:55:00+00:00", "detail": "prompt"}


def test_record_start_survives_an_unwritable_ledger(cfg, monkeypatch):
    def unwritable(_event):
        raise PermissionError("locked")

    monkeypatch.setattr(store, "append", unwritable)
    cli._record_start(time(8, 55), "flag", _TUESDAY_10_LOCAL, cfg)


def test_a_start_typed_at_the_first_launch_survives_a_relaunch_on_the_silent_late_branch(cfg, monkeypatch):
    """2 October: a start typed at the first launch was re-derived at a later launch and moved. Now it is recorded and read back, and the relaunch re-derives only when nothing was recorded."""
    monkeypatch.setattr(daemon, "system_boot_time", lambda: None)
    args = cli.build_parser().parse_args([])
    first = cli._resolve_start(args, cfg, _at(10, 5, day=29), read=lambda _p: "08:55", write=lambda _t: None)
    assert first == DayStart(time(8, 55), Provenance.PROMPT)

    store.append(_ledger_event(_utc(29, 7, 33), "collector_start", reason="launch"))
    store.append(_ledger_event(_utc(29, 7, 33, 30), "unlock", Source.EVENTLOG))  # 09:33:30 local
    relaunch = cli._resolve_start(args, cfg, _at(20, 0, day=29), read=_unexpected_read)
    assert relaunch == DayStart(time(8, 55), Provenance.PROMPT)

    store.shard_path(paths.current_host()).unlink()
    store.append(_ledger_event(_utc(29, 7, 33), "collector_start", reason="launch"))
    store.append(_ledger_event(_utc(29, 7, 33, 30), "unlock", Source.EVENTLOG))
    assert cli._resolve_start(args, cfg, _at(20, 0, day=29), read=_unexpected_read) == DayStart(
        time(9, 33), Provenance.OBSERVED
    )


def test_the_evidence_is_taken_without_a_prompt_and_only_the_flag_overrides_it(cfg, monkeypatch):
    """2 October under step 8: the 09:33 unlock is the start without asking, so the typed 08:55 goes through `--start`, and the relaunch reads it back."""
    monkeypatch.setattr(daemon, "system_boot_time", lambda: None)
    store.append(_ledger_event(_utc(29, 7, 33), "collector_start", reason="launch"))
    store.append(_ledger_event(_utc(29, 7, 33, 30), "unlock", Source.EVENTLOG))
    plain = cli.build_parser().parse_args([])
    assert cli._resolve_start(plain, cfg, _at(10, 5, day=29), read=_unexpected_read) == DayStart(
        time(9, 33), Provenance.OBSERVED
    )

    flagged = cli.build_parser().parse_args(["--start", "08:55"])
    assert cli._resolve_start(flagged, cfg, _at(10, 5, day=29), read=_unexpected_read) == DayStart(
        time(8, 55), Provenance.FLAG
    )
    assert cli._resolve_start(plain, cfg, _at(20, 0, day=29), read=_unexpected_read) == DayStart(
        time(8, 55), Provenance.FLAG
    )


def test_start_flag_wins_over_a_start_confirmed_earlier_and_is_read_back(cfg, monkeypatch):
    monkeypatch.setattr(daemon, "system_boot_time", lambda: None)
    plain = cli.build_parser().parse_args([])
    cli._resolve_start(plain, cfg, _at(10, 5, day=29), read=lambda _p: "08:55", write=lambda _t: None)
    flagged = cli.build_parser().parse_args(["--start", "08:30"])
    assert cli._resolve_start(flagged, cfg, _at(10, 13, day=29), read=_unexpected_read) == DayStart(
        time(8, 30), Provenance.FLAG
    )
    assert cli._resolve_start(plain, cfg, _at(10, 20, day=29), read=_unexpected_read) == DayStart(
        time(8, 30), Provenance.FLAG
    )


def test_a_start_is_not_recorded_under_a_fixed_clock(cfg, monkeypatch):
    monkeypatch.setattr(daemon, "system_boot_time", lambda: None)
    fixed = cli.build_parser().parse_args(["--at", "2026-09-29 10:05", "--start", "08:30"])
    assert cli._resolve_start(fixed, cfg, _TUESDAY_10_LOCAL, read=_unexpected_read) == DayStart(
        time(8, 30), Provenance.FLAG
    )
    prompted = cli.build_parser().parse_args(["--at", "2026-09-29 10:05"])
    answer = cli._resolve_start(
        prompted, cfg, _TUESDAY_10_LOCAL, read=lambda _p: "08:40", write=lambda _t: None
    )
    assert answer == DayStart(time(8, 40), Provenance.PROMPT)
    assert not store.shard_path(paths.current_host()).exists()

    live = cli.build_parser().parse_args(["--start", "08:30"])
    cli._resolve_start(live, cfg, _TUESDAY_10_LOCAL, read=_unexpected_read)
    assert len(store.read_shard(store.shard_path(paths.current_host())).events) == 1


def test_prompt_shows_and_accepts_the_suggested_default(cfg):
    prompts: list[str] = []

    def read(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    assert cli._prompt_start(cfg, default=time(8, 12), read=read) == time(8, 12)
    assert prompts == ["Start time [08:12]: "]


def test_prompt_override_beats_the_suggested_default(cfg):
    assert cli._prompt_start(cfg, default=time(8, 12), read=lambda _: "08:40") == time(8, 40)


@pytest.mark.parametrize("provenance", [Provenance.OBSERVED, Provenance.INFERRED, Provenance.LOGON])
def test_resolve_start_takes_the_ledger_start_without_prompting_or_recording(cfg, provenance):
    args = cli.build_parser().parse_args([])
    recorded, record = _recorder()
    known = DayStart(time(8, 12), provenance)
    start = cli._resolve_start(
        args, cfg, _TUESDAY_10_LOCAL, read=_unexpected_read, suggest=lambda _now: known, record=record
    )
    assert start == known
    assert recorded == []


def test_resolve_start_prompts_with_the_config_default_when_the_ledger_has_nothing(cfg):
    """An empty answer confirms the default, and a confirmed start is a fact: it is recorded like a typed one."""
    args = cli.build_parser().parse_args([])
    prompts: list[str] = []
    recorded, record = _recorder()

    def read(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    start = cli._resolve_start(
        args, cfg, _TUESDAY_10_LOCAL, read=read, suggest=_NO_LEDGER_START, record=record
    )
    assert start == DayStart(cfg.credit.default_start, Provenance.PROMPT)
    assert prompts == [f"Start time [{cfg.credit.default_start:%H:%M}]: "]
    assert recorded == [(cfg.credit.default_start, "prompt")]


def test_resolve_start_reads_no_ledger_for_a_flag_or_a_weekend(cfg):
    never = lambda _now: pytest.fail("must not consult the ledger")  # noqa: E731
    given = cli.build_parser().parse_args(["--start", "08:30"])
    assert cli._resolve_start(given, cfg, _TUESDAY_10_LOCAL, suggest=never, record=_discard) == DayStart(
        time(8, 30), Provenance.FLAG
    )
    saturday = datetime(2026, 9, 26, 10, 0, tzinfo=cli.local_timezone())
    plain = cli.build_parser().parse_args([])
    assert cli._resolve_start(plain, cfg, saturday, suggest=never, record=_discard) == DayStart(
        cfg.credit.default_start, Provenance.INFERRED
    )


@pytest.mark.parametrize("hour", [20, 23, 2])
def test_silent_late_and_night_branches_use_the_ledger_start(cfg, hour):
    args = cli.build_parser().parse_args([])
    known = DayStart(time(9, 15), Provenance.OBSERVED)
    recorded, record = _recorder()
    start = cli._resolve_start(
        args, cfg, _at(hour), read=_unexpected_read, suggest=lambda _now: known, record=record
    )
    assert start == known
    assert recorded == []


@pytest.mark.parametrize("hour", [20, 23, 2])
def test_silent_late_and_night_branches_fall_back_without_a_ledger_start(cfg, hour):
    args = cli.build_parser().parse_args([])
    recorded, record = _recorder()
    start = cli._resolve_start(
        args, cfg, _at(hour), read=_unexpected_read, suggest=_NO_LEDGER_START, record=record
    )
    assert start == DayStart(cfg.credit.default_start, Provenance.INFERRED)
    assert recorded == []


def test_silent_late_branch_reads_the_real_ledger(cfg, monkeypatch):
    monkeypatch.setattr(daemon, "system_boot_time", lambda: None)
    store.append(_ledger_event(_utc(14, 7, 15), "collector_start", reason="launch"))
    store.append(_ledger_event(_utc(14, 7, 15), "unlock", Source.EVENTLOG))  # 09:15 local
    args = cli.build_parser().parse_args([])
    assert cli._resolve_start(args, cfg, _at(20, 0), read=_unexpected_read) == DayStart(
        time(9, 15), Provenance.OBSERVED
    )


def test_flex_warning_every_line_names_the_start_and_the_band():
    for line in _pool("start.outside_flex"):
        text = line.format(start="09:15", flex_start="08:00", flex_end="09:00")
        assert "09:15" in text
        assert "08:00" in text
        assert "09:00" in text
