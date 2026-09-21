from datetime import UTC, datetime
from io import StringIO

from rich.console import Console

from timeexisting.domain.clock import FixedClock
from timeexisting.ui import app
from timeexisting.ui.layout import build_layout
from timeexisting.ui.theme import load_theme


def test_localize_converts_utc_to_copenhagen_offset():
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)  # CET, UTC+1
    summer = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)  # CEST, UTC+2
    assert app._localize(winter).utcoffset().total_seconds() == 3600
    assert app._localize(summer).utcoffset().total_seconds() == 7200


def test_update_populates_every_dynamic_region_without_raising():
    theme = load_theme()
    layout = build_layout(theme)
    now = app._localize(datetime(2026, 9, 14, 10, 0, tzinfo=UTC))
    app._update(layout, now, theme)


def test_run_exits_cleanly_on_keyboard_interrupt_without_sleeping_on_exit(monkeypatch):
    clock = FixedClock(datetime(2026, 9, 14, 10, 0, tzinfo=UTC))
    console = Console(file=StringIO(), force_terminal=True, width=80)

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        raise KeyboardInterrupt

    monkeypatch.setattr(app.time, "sleep", fake_sleep)

    app.run(clock, refresh_hz=1.0, console=console)

    assert sleep_calls == [1.0]
    output = console.file.getvalue()
    assert "liberated" in output


def test_run_demo_steps_through_every_scenario_before_looping(monkeypatch):
    scenarios = (
        ("Pre-Work", datetime(2026, 9, 14, 6, 0, tzinfo=UTC)),
        ("Lunch", datetime(2026, 9, 14, 11, 30, tzinfo=UTC)),
    )
    console = Console(file=StringIO(), force_terminal=True, width=80)

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(app.time, "sleep", fake_sleep)

    app.run_demo(scenarios, step_seconds=1.0, refresh_hz=1.0, console=console)

    assert sleep_calls == [1.0, 1.0, 1.0]
    output = console.file.getvalue()
    assert "liberated" in output


def test_run_demo_footer_names_the_scenario_and_the_demo_tag(monkeypatch):
    scenarios = (("Lunch", datetime(2026, 9, 14, 11, 30, tzinfo=UTC)),)
    console = Console(file=StringIO(), force_terminal=True, width=80)

    theme = load_theme()
    layout = build_layout(theme)
    monkeypatch.setattr(app, "build_layout", lambda _theme: layout)

    def fake_sleep(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(app.time, "sleep", fake_sleep)

    app.run_demo(scenarios, step_seconds=1.0, refresh_hz=1.0, console=console)

    footer_text = layout["footer"].renderable.plain
    assert "Lunch" in footer_text
    assert "[DEMO]" in footer_text
