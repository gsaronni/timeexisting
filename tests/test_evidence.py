"""The collector's evidence tracker (phase 3 step 7), on synthetic events only."""

from datetime import UTC, datetime, timedelta

import pytest

from timeexisting.collector.evidence import Evidence
from timeexisting.ledger.events import Confidence, Event, EventType, Source
from timeexisting.presence.backend import SessionState

_T0 = datetime(2026, 9, 17, 6, 0, 0, tzinfo=UTC)
LOCKED = SessionState.LOCKED
UNLOCKED = SessionState.UNLOCKED


def _at(minutes: float) -> datetime:
    return _T0 + timedelta(minutes=minutes)


def _event(minutes: float, event: EventType, source: Source = Source.EVENTLOG, **data: str) -> Event:
    return Event.new(
        ts=_at(minutes),
        host="laptop",
        profile="work",
        event=event,
        source=source,
        confidence=Confidence.OBSERVED,
        data=data or None,
    )


def _stop(minutes: float) -> Event:
    return _event(minutes, EventType.COLLECTOR_STOP, Source.COLLECTOR, reason="unclean")


# The fold.


def test_a_resume_onto_an_unlocked_session_wants_input_from_the_resume():
    tracker = Evidence()
    tracker.started(_at(0), UNLOCKED)
    assert not tracker.wants_input()
    tracker.heard(_at(5), EventType.SUSPEND, Source.EVENTLOG)
    assert not tracker.wants_input()
    tracker.heard(_at(20), EventType.RESUME, Source.EVENTLOG)
    assert tracker.wants_input()
    assert tracker.unevidenced_since == _at(20)


@pytest.mark.parametrize(
    ("event", "source"),
    [(EventType.INPUT, Source.WIN32), (EventType.INPUT, Source.EVENTLOG), (EventType.UNLOCK, Source.WTS)],
)
def test_input_or_an_unlock_after_the_resume_evidences_the_stretch(event, source):
    tracker = Evidence()
    tracker.started(_at(0), UNLOCKED)
    tracker.heard(_at(5), EventType.SUSPEND, Source.EVENTLOG)
    tracker.heard(_at(20), EventType.RESUME, Source.EVENTLOG)
    tracker.heard(_at(21), event, source)
    assert not tracker.wants_input()
    assert tracker.unevidenced_since is None


def test_a_locked_session_never_wants_input():
    tracker = Evidence()
    tracker.started(_at(0), UNLOCKED)
    tracker.heard(_at(4), EventType.LOCK, Source.WTS)
    tracker.heard(_at(5), EventType.SUSPEND, Source.EVENTLOG)
    tracker.heard(_at(20), EventType.RESUME, Source.EVENTLOG)
    assert not tracker.wants_input()


def test_input_while_asleep_or_locked_evidences_nothing():
    tracker = Evidence()
    tracker.started(_at(0), UNLOCKED)
    tracker.heard(_at(5), EventType.SUSPEND, Source.EVENTLOG)
    tracker.heard(_at(10), EventType.INPUT, Source.WIN32)
    tracker.heard(_at(20), EventType.RESUME, Source.EVENTLOG)
    assert tracker.unevidenced_since == _at(20)


@pytest.mark.parametrize("session", [LOCKED, None])
def test_a_run_started_on_a_locked_or_unknown_session_wants_no_input(session):
    tracker = Evidence()
    tracker.started(_at(0), session)
    tracker.heard(_at(5), EventType.SUSPEND, Source.EVENTLOG)
    tracker.heard(_at(20), EventType.RESUME, Source.EVENTLOG)
    assert not tracker.wants_input()


def test_events_that_are_not_transitions_are_ignored():
    tracker = Evidence()
    tracker.started(_at(0), UNLOCKED)
    for event in (EventType.NOTE, EventType.COLLECTOR_STOP, EventType.DAY_START):
        tracker.heard(_at(1), event, Source.COLLECTOR)
    assert tracker.session_at(_at(2)) is UNLOCKED
    assert not tracker.wants_input()


def test_out_of_order_backfill_folds_into_place():
    """A sleep heard first from the clock, then a Winlogon lock from before it, backfilled: the lock during the standby wins."""
    tracker = Evidence()
    tracker.started(_at(0), UNLOCKED)
    tracker.heard(_at(25), EventType.SUSPEND, Source.CLOCK)
    tracker.heard(_at(30), EventType.RESUME, Source.CLOCK)
    assert tracker.wants_input()
    tracker.heard(_at(10), EventType.LOCK, Source.EVENTLOG)
    assert not tracker.wants_input()
    assert tracker.session_at(_at(30)) is LOCKED
    assert tracker.session_at(_at(9)) is UNLOCKED


def test_an_earlier_suspend_backfilled_after_a_resume_moves_the_stretch_start():
    tracker = Evidence()
    tracker.started(_at(0), UNLOCKED)
    tracker.heard(_at(30), EventType.SUSPEND, Source.EVENTLOG)
    tracker.heard(_at(40), EventType.RESUME, Source.EVENTLOG)
    tracker.heard(_at(5), EventType.SUSPEND, Source.EVENTLOG)
    tracker.heard(_at(6), EventType.RESUME, Source.EVENTLOG)
    assert tracker.unevidenced_since == _at(40)


# The session at a time.


def test_a_lock_and_a_wake_at_the_same_time_read_as_locked():
    tracker = Evidence([_event(0, EventType.UNLOCK), _event(5, EventType.SUSPEND)])
    tracker.heard(_at(20), EventType.RESUME, Source.EVENTLOG)
    tracker.heard(_at(20), EventType.LOCK, Source.EVENTLOG)
    assert tracker.session_at(_at(20)) is LOCKED
    assert tracker.session_at(_at(20) - timedelta(microseconds=1)) is UNLOCKED


def test_a_lock_at_the_same_time_wins_over_an_unlock_heard_after_it():
    tracker = Evidence([_event(0, EventType.LOCK)])
    tracker.heard(_at(20), EventType.LOCK, Source.WTS)
    tracker.heard(_at(20), EventType.UNLOCK, Source.WTS)
    assert tracker.session_at(_at(20)) is LOCKED


def test_the_session_is_unknown_before_any_session_record():
    tracker = Evidence([_event(5, EventType.SUSPEND), _event(6, EventType.UNLOCK)])
    assert tracker.session_at(_at(5)) is None


def test_the_run_start_carries_the_probed_session():
    tracker = Evidence([_event(0, EventType.LOCK)])
    tracker.started(_at(60), UNLOCKED)
    assert tracker.session_at(_at(59)) is LOCKED
    assert tracker.session_at(_at(60)) is UNLOCKED
    tracker.started(_at(120), None)
    assert tracker.session_at(_at(121)) is UNLOCKED


# Boots (step 7 review): replay's boot steps, and a boot without a kind breaks the session.


def _boot(minutes: float, kind: str | None) -> Event:
    data = {"detail": "System Kernel-General 12"} | ({} if kind is None else {"boot_kind": kind})
    return _event(minutes, EventType.BOOT, **data)


@pytest.mark.parametrize("booted", [True, False], ids=["boot-start", "no-boot"])
def test_the_session_is_locked_after_a_boot_start(booted):
    ledger = [_event(0, EventType.UNLOCK), *([_boot(10, "start")] if booted else [])]
    tracker = Evidence(ledger)
    assert tracker.session_at(_at(9)) is UNLOCKED
    assert tracker.session_at(_at(11)) is (LOCKED if booted else UNLOCKED)


@pytest.mark.parametrize("unlocked_after", [False, True], ids=["kindless-boot", "unlock-after-it"])
@pytest.mark.parametrize("kind", [None, "restart"], ids=["no-kind", "unknown-kind"])
def test_the_session_is_unknown_after_a_boot_without_a_kind(kind, unlocked_after):
    ledger = [_event(0, EventType.UNLOCK), _boot(10, kind)]
    if unlocked_after:
        ledger.append(_event(12, EventType.UNLOCK))
    tracker = Evidence(ledger)
    assert tracker.session_at(_at(11)) is None
    assert tracker.session_at(_at(13)) is (UNLOCKED if unlocked_after else None)


def test_a_boot_stop_leaves_the_session_and_turns_the_machine_off():
    tracker = Evidence([_event(0, EventType.UNLOCK), _boot(10, "stop")])
    assert tracker.session_at(_at(11)) is UNLOCKED
    # Off is not awake: even an input on the unlocked session evidences nothing and wants nothing.
    tracker.heard(_at(12), EventType.INPUT, Source.WIN32)
    assert not tracker.wants_input()


@pytest.mark.parametrize("kind", ["start", None])
@pytest.mark.parametrize("unlocked_after", [False, True], ids=["no-unlock", "unlock-after-the-boot"])
def test_after_a_boot_no_input_is_wanted_until_an_unlock(kind, unlocked_after):
    """Unlocked before a reboot, then a boot, a sleep and a resume: not unlocked, so no input wanted. The control unlocks after the boot, so the resume wants input."""
    ledger = [_event(0, EventType.UNLOCK), _boot(10, kind)]
    if unlocked_after:
        ledger.append(_event(12, EventType.UNLOCK))
    ledger += [_event(30, EventType.SUSPEND), _event(60, EventType.RESUME)]
    tracker = Evidence(ledger)
    assert tracker.unevidenced_since == (_at(60) if unlocked_after else None)


def test_a_boot_start_sets_the_machine_awake():
    """Off after a boot stop, awake after the boot start, but locked: an unlock then evidences at once."""
    tracker = Evidence([_event(0, EventType.UNLOCK), _boot(5, "stop"), _boot(10, "start")])
    assert not tracker.wants_input()
    tracker.heard(_at(11), EventType.UNLOCK, Source.EVENTLOG)
    assert tracker.session_at(_at(11)) is UNLOCKED
    assert not tracker.wants_input()
    tracker.heard(_at(20), EventType.SUSPEND, Source.EVENTLOG)
    tracker.heard(_at(30), EventType.RESUME, Source.EVENTLOG)
    assert tracker.unevidenced_since == _at(30)


def test_heard_takes_the_boot_kind():
    tracker = Evidence([_event(0, EventType.UNLOCK)])
    tracker.heard(_at(10), EventType.BOOT, Source.COLLECTOR, "start")
    assert tracker.session_at(_at(11)) is LOCKED
    tracker.heard(_at(20), EventType.UNLOCK, Source.WTS)
    tracker.heard(_at(30), EventType.BOOT, Source.EVENTLOG)
    assert tracker.session_at(_at(31)) is None


# Seeding.


def test_seeding_looks_through_a_collector_stop():
    """An unclean stop leaves the session as the last record before it said: a 507 backfilled into the gap reads unlocked."""
    ledger = [
        _event(0, EventType.UNLOCK),
        _event(30, EventType.SUSPEND),
        _event(60, EventType.RESUME),
        _stop(61),
    ]
    tracker = Evidence.seed(ledger, _at(61))
    assert tracker.session_at(_at(90)) is UNLOCKED


def test_seeding_carries_a_lock_from_before_the_last_sleep():
    ledger = [
        _event(0, EventType.UNLOCK),
        _event(10, EventType.LOCK),
        _event(30, EventType.SUSPEND),
        _event(60, EventType.RESUME),
        _stop(61),
    ]
    tracker = Evidence.seed(ledger, _at(61))
    assert tracker.session_at(_at(90)) is LOCKED


def test_seeding_takes_the_last_win32_input_from_the_whole_ledger():
    ledger = [
        _event(0, EventType.INPUT, Source.WIN32),
        _event(1, EventType.INPUT, Source.EVENTLOG, detail="System Kernel-Power 507 reason 32"),
        _event(2, EventType.UNLOCK),
        _event(30, EventType.SUSPEND),
        _event(60, EventType.RESUME),
    ]
    assert Evidence.seed(ledger, _at(60)).last_win32 == _at(0)
    assert Evidence.seed([], _at(0)).last_win32 is None


def _long_ledger() -> list[Event]:
    """Three weeks of days: unlock, a midday lock and sleep, an afternoon unlock, a stop, an evening lid close on an unlocked session."""
    events: list[Event] = []
    for day in range(21):
        base = day * 24 * 60
        events += [
            _event(base + 8 * 60, EventType.RESUME),
            _event(base + 8 * 60 + 1, EventType.UNLOCK),
            _event(base + 8 * 60 + 30, EventType.INPUT, Source.WIN32),
            _event(base + 12 * 60, EventType.LOCK),
            _event(base + 12 * 60 + 5, EventType.SUSPEND),
            _event(base + 13 * 60, EventType.RESUME),
            _event(base + 13 * 60 + 1, EventType.UNLOCK),
            _stop(base + 15 * 60),
            _event(base + 15 * 60 + 30, EventType.COLLECTOR_START, Source.COLLECTOR, reason="launch"),
            _event(base + 17 * 60, EventType.SUSPEND),
        ]
    return events


@pytest.mark.parametrize(
    "anchor",
    [
        _at(20 * 24 * 60 + 17 * 60),  # on the last suspend
        _at(20 * 24 * 60 + 12 * 60 + 30),  # inside the last midday sleep
        _at(20 * 24 * 60 + 9 * 60),  # on the last morning
        _at(25 * 24 * 60),  # after the whole ledger
    ],
)
def test_the_bounded_seed_answers_like_a_full_read(anchor):
    ledger = _long_ledger()
    full = Evidence(ledger)
    bounded = Evidence.seed(ledger, anchor)
    assert len(bounded._entries) < len(full._entries)
    assert bounded.last_win32 == full.last_win32
    last_machine = _at(20 * 24 * 60 + 17 * 60)
    bound = min(anchor, last_machine)
    probes = [bound + timedelta(minutes=m) for m in range(0, 6 * 60, 7)]
    assert [bounded.session_at(t) for t in probes] == [full.session_at(t) for t in probes]
    assert bounded.unevidenced_since == full.unevidenced_since
    for tracker in (full, bounded):
        tracker.heard(_at(21 * 24 * 60 + 8 * 60), EventType.RESUME, Source.EVENTLOG)
    assert bounded.unevidenced_since == full.unevidenced_since == _at(21 * 24 * 60 + 8 * 60)
    for tracker in (full, bounded):
        tracker.started(_at(21 * 24 * 60 + 9 * 60), UNLOCKED)
    assert bounded.wants_input() == full.wants_input() is False


def _rebooting_ledger(evening: str) -> list[Event]:
    """`_long_ledger`, whose last evening closes the lid on an unlocked session, then reboots: a boot stop and start (`restart`) or one boot without a kind (`kindless`)."""
    last = 20 * 24 * 60
    boots = {
        "restart": [_boot(last + 18 * 60, "stop"), _boot(last + 18 * 60 + 2, "start")],
        "kindless": [_boot(last + 18 * 60, None)],
    }
    return [*_long_ledger(), *boots[evening]]


@pytest.mark.parametrize("evening", ["restart", "kindless"])
@pytest.mark.parametrize(
    "anchor",
    [
        _at(20 * 24 * 60 + 18 * 60 + 2),  # on the boot start
        _at(20 * 24 * 60 + 12 * 60 + 30),  # inside the last midday sleep: the boot is inside the slice
        _at(20 * 24 * 60 + 9 * 60),  # on the last morning
        _at(25 * 24 * 60),  # after the whole ledger
    ],
)
def test_the_bounded_seed_with_a_boot_answers_like_a_full_read(evening, anchor):
    """A collector restarted after a reboot answers like one that ran through it: the boot resets the session in the bounded slice too, so the next resume wants no input."""
    ledger = _rebooting_ledger(evening)
    full = Evidence(ledger)
    bounded = Evidence.seed(ledger, anchor)
    assert len(bounded._entries) < len(full._entries)
    assert any(entry.event is EventType.BOOT for entry in bounded._entries)
    # The bound: the anchor, or the last record that fixes the machine when that is earlier. A boot start fixes it, a kindless boot does not.
    last_machine = _at(20 * 24 * 60 + (18 * 60 + 2 if evening == "restart" else 17 * 60))
    probes = [min(anchor, last_machine) + timedelta(minutes=m) for m in range(0, 8 * 60, 7)]
    assert [bounded.session_at(t) for t in probes] == [full.session_at(t) for t in probes]
    after = _at(20 * 24 * 60 + 18 * 60 + 5)
    assert bounded.session_at(after) is full.session_at(after) is (LOCKED if evening == "restart" else None)
    for tracker in (full, bounded):
        tracker.heard(_at(21 * 24 * 60 + 8 * 60), EventType.RESUME, Source.EVENTLOG)
    assert bounded.unevidenced_since is full.unevidenced_since is None
    # Control: the same ledger without the reboot wants input at that resume.
    plain = Evidence.seed(_long_ledger(), anchor)
    plain.heard(_at(21 * 24 * 60 + 8 * 60), EventType.RESUME, Source.EVENTLOG)
    assert plain.unevidenced_since == _at(21 * 24 * 60 + 8 * 60)
