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
