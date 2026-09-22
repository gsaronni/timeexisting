from datetime import UTC, date, datetime, time
from io import StringIO

from rich.console import Console

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
