from datetime import UTC, date, datetime, time
from io import StringIO

import pytest
from rich.console import Console

from timeexisting.content import phrases
from timeexisting.domain.clock import FixedClock
from timeexisting.domain.resolver import resolve
from timeexisting.domain.schedule import build_day
from timeexisting.domain.timeline_view import CollectorState
from timeexisting.ledger.replay import IntervalType
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


def test_update_marks_an_inferred_start_on_the_day_panel(cfg):
    theme = load_theme()
    layout = build_layout(theme)
    resolved = _resolved(cfg, datetime(2026, 9, 14, 10, 0, tzinfo=app.local_timezone()))
    app._update(layout, resolved, cfg, theme, start_inferred=True)
    console = Console(file=StringIO(), width=120, record=True, legacy_windows=False)
    console.print(layout["main"]["right"].renderable)
    assert "(inferred)" in console.export_text()


def test_run_passes_the_inferred_start_to_the_day_panel(cfg, monkeypatch):
    seen: list[bool] = []

    def spy(*_positional, start_inferred=False):
        seen.append(start_inferred)
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "_update", spy)
    clock = FixedClock(datetime(2026, 9, 14, 10, 0, tzinfo=UTC))
    console = Console(file=StringIO(), force_terminal=True, width=80)
    app.run(clock, cfg, time(8, 30), frozenset(), console=console, start_inferred=True)
    app.run(clock, cfg, time(8, 30), frozenset(), console=console)
    assert seen == [True, False]


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


_SINCE = datetime(2026, 9, 28, 6, 5, tzinfo=UTC)  # 08:05 in Copenhagen, summer time


def test_collector_line_reports_the_collectors_start_when_the_ledger_has_no_open_interval():
    theme = load_theme()
    line = app._collector_line(CollectorState(running=True, kind=None, since=_SINCE), False, theme)
    assert line.plain == phrases.pick("app.collector.alive").format(since="08:05")
    assert line.style == theme.collector_alive


def test_collector_line_distinguishes_absent_from_a_failed_spawn():
    theme = load_theme()
    gone = CollectorState(running=False, kind=None, since=None)
    absent = app._collector_line(gone, False, theme)
    failed = app._collector_line(gone, True, theme)
    assert absent.plain == phrases.pick("app.collector.absent")
    assert failed.plain == phrases.pick("app.collector.spawn_failed")
    assert absent.style == failed.style == theme.collector_absent


def test_collector_line_says_not_observing_since_the_last_checkpoint():
    theme = load_theme()
    line = app._collector_line(CollectorState(running=False, kind=None, since=_SINCE), False, theme)
    assert line.plain == phrases.pick("app.collector.not_observing").format(since="08:05")
    assert line.style == theme.collector_absent
    # A failed spawn still reads as one, whatever checkpoint an older collector left.
    failed = app._collector_line(CollectorState(running=False, kind=None, since=_SINCE), True, theme)
    assert failed.plain == phrases.pick("app.collector.spawn_failed")


@pytest.mark.parametrize(
    ("kind", "key"),
    [
        (IntervalType.PRESENT, "present"),
        (IntervalType.AWAY, "away"),
        (IntervalType.ASLEEP, "asleep"),
        (IntervalType.UNEVIDENCED, "unevidenced"),
    ],
)
def test_collector_line_shows_the_derived_state_and_since_when(kind, key):
    theme = load_theme()
    line = app._collector_line(CollectorState(running=True, kind=kind, since=_SINCE), False, theme)
    assert line.plain == phrases.pick(f"app.collector.state.{key}").format(since="08:05")
    assert line.style == theme.state[key]


def test_collector_line_falls_back_to_the_alive_line_for_a_state_without_a_phrase():
    """`downtime` and `off` never stay open while the viewer runs; if one did, the line says the collector is alive rather than inventing a state."""
    theme = load_theme()
    line = app._collector_line(
        CollectorState(running=True, kind=IntervalType.DOWNTIME, since=_SINCE), False, theme
    )
    assert line.plain == phrases.pick("app.collector.alive").format(since="08:05")
    assert line.style == theme.collector_alive


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
    gone = CollectorState(running=False, kind=None, since=None)
    answers = iter([gone, CollectorState(running=True, kind=IntervalType.PRESENT, since=since), gone])
    footers = _run_capturing_footers(
        cfg, monkeypatch, 3, collector_state=lambda: next(answers), spawn_failed=True
    )
    lines = [footer.split("\n")[1] for footer in footers]
    assert lines[0] == phrases.pick("app.collector.spawn_failed")
    assert lines[1] == phrases.pick("app.collector.state.present").format(since="08:30")
    # Seen once, then gone: a stop, not a failed spawn.
    assert lines[2] == phrases.pick("app.collector.absent")


def test_run_returns_on_q_without_an_interrupt(cfg, monkeypatch):
    clock = FixedClock(datetime(2026, 10, 7, 9, 0, tzinfo=UTC))
    console = Console(file=StringIO(), force_terminal=True, width=80)
    pressed = iter(["q"])
    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) > 3:
            pytest.fail("q did not stop the loop")

    monkeypatch.setattr(app.time, "sleep", fake_sleep)
    app.run(clock, cfg, time(8, 30), frozenset(), console=console, keys=lambda: next(pressed, None))

    assert sleep_calls == [1.0]
    output = console.file.getvalue()
    assert phrases.pick("app.quit") in output
    assert "liberated" not in output


def test_run_drains_every_pending_key_and_only_lowercase_q_quits(cfg, monkeypatch):
    """Two keys in one tick, neither of them `q`, are dropped, `Q` included; the `q` on the next tick quits."""
    clock = FixedClock(datetime(2026, 10, 7, 9, 0, tzinfo=UTC))
    console = Console(file=StringIO(), force_terminal=True, width=80)
    pressed = iter(["Q", "x", None, "q", None])
    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) > 3:
            pytest.fail("q did not stop the loop")

    monkeypatch.setattr(app.time, "sleep", fake_sleep)
    app.run(clock, cfg, time(8, 30), frozenset(), console=console, keys=lambda: next(pressed, None))

    assert sleep_calls == [1.0, 1.0]
    assert phrases.pick("app.quit") in console.file.getvalue()


def test_run_demo_returns_on_q_without_an_interrupt(cfg, monkeypatch):
    scenarios = (("Lunch", _resolved(cfg, datetime(2026, 9, 14, 11, 5, tzinfo=app.local_timezone()))),)
    console = Console(file=StringIO(), force_terminal=True, width=80)
    pressed = iter([None, "q"])
    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) > 3:
            pytest.fail("q did not stop the demo")

    monkeypatch.setattr(app.time, "sleep", fake_sleep)
    app.run_demo(
        scenarios, cfg, step_seconds=4.0, refresh_hz=1.0, console=console, keys=lambda: next(pressed, None)
    )

    assert sleep_calls == [1.0, 1.0]
    output = console.file.getvalue()
    assert phrases.pick("app.quit") in output
    assert "liberated" not in output
