from datetime import UTC, date, datetime, time
from io import StringIO

from rich.console import Console

from timeexisting.content import phrases
from timeexisting.domain.clock import FixedClock
from timeexisting.domain.resolver import resolve
from timeexisting.domain.schedule import build_day
from timeexisting.ui import app
from timeexisting.ui.layout import build_layout
from timeexisting.ui.theme import load_theme

_MONDAY = date(2026, 9, 14)


def _resolved(cfg, moment: datetime):
    plan = build_day(_MONDAY, time(8, 30), frozenset(), cfg)
    return resolve(moment, plan)


def test_localize_converts_utc_to_copenhagen_offset():
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    summer = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
    assert app._localize(winter).utcoffset().total_seconds() == 3600
    assert app._localize(summer).utcoffset().total_seconds() == 7200


def test_update_populates_every_dynamic_region_without_raising(cfg):
    theme = load_theme()
    layout = build_layout(theme)
    resolved = _resolved(cfg, datetime(2026, 9, 14, 10, 0, tzinfo=app.local_timezone()))
    app._update(layout, resolved, cfg, theme)


def test_run_exits_cleanly_on_keyboard_interrupt_without_sleeping_on_exit(cfg, monkeypatch):
    clock = FixedClock(datetime(2026, 9, 14, 10, 0, tzinfo=UTC))
    console = Console(file=StringIO(), force_terminal=True, width=80)

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(app.time, "sleep", fake_sleep)

    app.run(clock, cfg, time(8, 30), frozenset(), refresh_hz=1.0, console=console)

    assert sleep_calls == [1.0]
    output = console.file.getvalue()
    assert "liberated" in output


def test_run_demo_steps_through_every_scenario_before_looping(cfg, monkeypatch):
    scenarios = (
        ("Pre-Work", _resolved(cfg, datetime(2026, 9, 14, 6, 0, tzinfo=app.local_timezone()))),
        ("Lunch", _resolved(cfg, datetime(2026, 9, 14, 11, 5, tzinfo=app.local_timezone()))),
    )
    console = Console(file=StringIO(), force_terminal=True, width=80)

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(app.time, "sleep", fake_sleep)

    app.run_demo(scenarios, cfg, step_seconds=1.0, refresh_hz=1.0, console=console)

    assert sleep_calls == [1.0, 1.0, 1.0]
    output = console.file.getvalue()
    assert "liberated" in output


def test_run_demo_footer_names_the_scenario_and_the_demo_tag(cfg, monkeypatch):
    scenarios = (("Lunch", _resolved(cfg, datetime(2026, 9, 14, 11, 5, tzinfo=app.local_timezone()))),)
    console = Console(file=StringIO(), force_terminal=True, width=80)

    theme = load_theme()
    layout = build_layout(theme)
    monkeypatch.setattr(app, "build_layout", lambda _theme: layout)

    def fake_sleep(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(app.time, "sleep", fake_sleep)

    app.run_demo(scenarios, cfg, step_seconds=1.0, refresh_hz=1.0, console=console)

    footer_text = layout["footer"].renderable.plain
    assert "Lunch" in footer_text
    assert "[DEMO]" in footer_text


def test_collector_line_reports_the_start_time_in_local_hours():
    theme = load_theme()
    since = datetime(2026, 9, 28, 6, 5, tzinfo=UTC)  # 08:05 in Copenhagen, summer time
    line = app._collector_line(since, False, theme)
    assert "08:05" in line.plain
    assert line.style == theme.collector_alive


def test_collector_line_distinguishes_absent_from_a_failed_spawn():
    theme = load_theme()
    absent = app._collector_line(None, False, theme)
    failed = app._collector_line(None, True, theme)
    assert absent.plain == phrases.pick("app.collector.absent")
    assert failed.plain == phrases.pick("app.collector.spawn_failed")
    assert absent.style == failed.style == theme.collector_absent


def _run_capturing_footers(cfg, monkeypatch, ticks, **kwargs):
    theme = load_theme()
    layout = build_layout(theme)
    monkeypatch.setattr(app, "build_layout", lambda _theme: layout)
    footers: list[str] = []

    def fake_sleep(_seconds):
        footers.append(layout["footer"].renderable.plain)
        if len(footers) >= ticks:
            raise KeyboardInterrupt

    monkeypatch.setattr(app.time, "sleep", fake_sleep)
    clock = FixedClock(datetime(2026, 9, 14, 10, 0, tzinfo=UTC))
    console = Console(file=StringIO(), force_terminal=True, width=80)
    app.run(clock, cfg, time(8, 30), frozenset(), console=console, **kwargs)
    return footers


def test_run_without_a_collector_callable_keeps_a_one_line_footer(cfg, monkeypatch):
    [footer] = _run_capturing_footers(cfg, monkeypatch, 1)
    assert "\n" not in footer


def test_run_footer_follows_the_collector_each_tick(cfg, monkeypatch):
    since = datetime(2026, 9, 14, 6, 30, tzinfo=UTC)  # 08:30 local
    answers = iter([None, since, None])
    footers = _run_capturing_footers(
        cfg, monkeypatch, 3, collector_since=lambda: next(answers), spawn_failed=True
    )
    lines = [footer.split("\n")[1] for footer in footers]
    assert lines[0] == phrases.pick("app.collector.spawn_failed")
    assert "08:30" in lines[1]
    # Seen once, then gone: a stop, not a failed spawn.
    assert lines[2] == phrases.pick("app.collector.absent")
