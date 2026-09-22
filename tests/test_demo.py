"""The demo carousel: `cli._demo_scenarios` must contain every phase and
every month, since `te --demo` is the one place all of them are showcased
together.
"""

from datetime import UTC, datetime

from timeexisting import cli
from timeexisting.domain.segments import Phase

_REFERENCE = datetime(2026, 9, 17, 15, 0, tzinfo=UTC)  # a Thursday


def test_demo_start_is_monday_of_the_reference_week():
    start = cli._demo_start(_REFERENCE)
    local_start = start.astimezone(cli.local_timezone())
    assert local_start.weekday() == 0
    assert local_start.date() < _REFERENCE.astimezone(cli.local_timezone()).date()


def test_demo_scenarios_contain_every_phase(cfg):
    scenarios = cli._demo_scenarios(_REFERENCE, cfg)
    phases_seen = {resolved.segment.phase for _label, resolved in scenarios}
    assert phases_seen == set(Phase)


def test_demo_scenarios_weekday_block_matches_labels_and_phases_in_order(cfg):
    scenarios = cli._demo_scenarios(_REFERENCE, cfg)
    labels = [label for label, _resolved in scenarios]

    assert labels[:9] == [
        "Off Hours",
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
        Phase.OFF_HOURS,
        Phase.PRE_WORK,
        Phase.WORKING,
        Phase.LUNCH,
        Phase.WORKING,
        Phase.WORKING,  # final stretch: still working, five minutes before nominal_end
        Phase.POST_WORK,
        Phase.OFF_DAY,
        Phase.OFF_DAY,
    ]
    for (_label, resolved), expected_phase in zip(scenarios[:9], expected_phases, strict=True):
        assert resolved.segment.phase is expected_phase


def test_demo_scenarios_weekend_entries_land_on_saturday_and_sunday(cfg):
    scenarios = cli._demo_scenarios(_REFERENCE, cfg)
    assert scenarios[7][1].now.weekday() == 5  # Saturday
    assert scenarios[8][1].now.weekday() == 6  # Sunday


def test_demo_scenarios_cover_every_month_on_a_weekday_at_noon(cfg):
    scenarios = cli._demo_scenarios(_REFERENCE, cfg)
    month_scenarios = scenarios[9:]

    assert len(month_scenarios) == 12
    months_seen = set()
    for label, resolved in month_scenarios:
        assert resolved.now.weekday() < 5
        assert resolved.now.hour == 12
        assert label != ""
        months_seen.add(resolved.now.month)
    assert months_seen == set(range(1, 13))
