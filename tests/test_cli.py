from datetime import UTC, datetime

import pytest

from timeexisting import cli
from timeexisting.domain.clock import FixedClock, ScaledClock, SystemClock


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


def test_demo_yields_a_scaled_clock_starting_monday_midnight_local():
    args = cli.build_parser().parse_args(["--demo"])
    clock, label = cli._build_clock(args)
    assert isinstance(clock, ScaledClock)
    assert label != ""

    local_start = clock.now().astimezone(cli.local_timezone())
    assert local_start.weekday() == 0
    assert (local_start.hour, local_start.minute, local_start.second) == (0, 0, 0)


def test_demo_start_is_monday_of_the_reference_week():
    reference = datetime(2026, 9, 17, 15, 0, tzinfo=UTC)  # a Thursday
    start = cli._demo_start(reference)
    local_start = start.astimezone(cli.local_timezone())
    assert local_start.weekday() == 0
    assert local_start.date() < reference.astimezone(cli.local_timezone()).date()
