"""The pure event log parser (roadmap sections 6 and 8) against hand-written XML fixtures.

The fixtures under `fixtures/eventlog/` follow the element and field layout of the real records on the work laptop at phase 3 step 0, with every value invented: no real host, SID, record number or time. Sequences are built from the same fixtures with the time and the fields under test substituted.
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from timeexisting.ledger.events import EventType
from timeexisting.presence.eventlog import (
    CHAIN_STEP,
    WAKE_REPORT_LAG,
    BootKind,
    BootRecord,
    Boundary,
    ClockChange,
    EventLogFormatError,
    HibernateEntry,
    PowerTransition,
    SessionRecord,
    WakeReport,
    boundaries,
    parse_system,
    parse_winlogon,
    system_xpath,
    winlogon_xpath,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "eventlog"
_USER = "S-1-5-21-1000000001-1000000002-1000000003-1001"
_OTHER_USER = "S-1-5-21-1000000001-1000000002-1000000003-1002"
_SYSTEM_SID = "S-1-5-18"

SUSPEND, RESUME, BOOT = EventType.SUSPEND, EventType.RESUME, EventType.BOOT
LOCK, UNLOCK = EventType.LOCK, EventType.UNLOCK


def _utc(day: int, hour: int, minute: int, second: float = 0, month: int = 9) -> datetime:
    whole = int(second)
    return datetime(2026, month, day, hour, minute, whole, round((second - whole) * 1_000_000), tzinfo=UTC)


def _system_time(ts: datetime) -> str:
    """As the event log writes it: UTC, seven fractional digits, `Z`."""
    return f"{ts:%Y-%m-%dT%H:%M:%S.%f}0Z"


def _fixture(
    name: str,
    *,
    ts: datetime | None = None,
    event_id: int | None = None,
    user: str | None = None,
    **data: object,
) -> str:
    xml = (_FIXTURES / name).read_text(encoding="utf-8")
    if ts is not None:
        xml, count = re.subn(r"SystemTime='[^']*'", f"SystemTime='{_system_time(ts)}'", xml)
        assert count == 1
    if event_id is not None:
        xml, count = re.subn(r"(<EventID[^>]*>)\d+(</EventID>)", rf"\g<1>{event_id}\g<2>", xml)
        assert count == 1
    if user is not None:
        xml, count = re.subn(r"UserID='[^']*'", f"UserID='{user}'", xml)
        assert count == 1
    for key, value in data.items():
        text = _system_time(value) if isinstance(value, datetime) else str(value)
        xml, count = re.subn(rf"(<Data Name='{key}'>)[^<]*(</Data>)", rf"\g<1>{text}\g<2>", xml)
        assert count == 1, key
    return xml


def _entry(ts: datetime, reason: int = 12) -> str:
    return _fixture("kernel-power-506.xml", ts=ts, Reason=reason)


def _exit(ts: datetime, reason: int = 32) -> str:
    return _fixture("kernel-power-507.xml", ts=ts, Reason=reason)


def _hibernate(ts: datetime, reason: int = 13) -> str:
    return _fixture("kernel-power-42.xml", ts=ts, Reason=reason)


def _wake_report(ts: datetime, sleep: datetime, wake: datetime) -> str:
    return _fixture("power-troubleshooter-1.xml", ts=ts, SleepTime=sleep, WakeTime=wake)


def _clock(ts: datetime, old: datetime, new: datetime, reason: int = 2) -> str:
    return _fixture("kernel-general-1.xml", ts=ts, OldTime=old, NewTime=new, Reason=reason)


def _winlogon(code: int, subscriber: str = "Sens", user: str = _USER, ts: datetime | None = None) -> str:
    return _fixture("winlogon-811.xml", ts=ts, user=user, Event=code, SubscriberName=subscriber)


def _records(*xmls: str):
    parsed = [parse_system(xml) for xml in xmls]
    return [record for record in parsed if record is not None]


def _at(*xmls: str, until: datetime) -> list[Boundary]:
    return boundaries(_records(*xmls), until=until)


def _shape(result: list[Boundary]) -> list[tuple[EventType, datetime, str]]:
    return [(boundary.event, boundary.time, boundary.detail) for boundary in result]


# Parsing, one fixture per confirmed record.


def test_506_fixture_parses_with_its_reason_and_the_100ns_digit_truncated():
    record = parse_system((_FIXTURES / "kernel-power-506.xml").read_text(encoding="utf-8"))
    assert record == PowerTransition(datetime(2026, 9, 14, 14, 32, 7, 123456, tzinfo=UTC), 506, 12)
    assert record.time.tzinfo is UTC


def test_507_fixture_parses_with_its_reason():
    record = parse_system((_FIXTURES / "kernel-power-507.xml").read_text(encoding="utf-8"))
    assert record == PowerTransition(datetime(2026, 9, 15, 5, 58, 41, 765432, tzinfo=UTC), 507, 32)


def test_42_fixture_parses_as_hibernation_entry():
    record = parse_system((_FIXTURES / "kernel-power-42.xml").read_text(encoding="utf-8"))
    assert record == HibernateEntry(_utc(15, 6, 0, 11.25), reason=13, target_state=5)


def test_power_troubleshooter_fixture_parses_sleep_and_wake_times():
    record = parse_system((_FIXTURES / "power-troubleshooter-1.xml").read_text(encoding="utf-8"))
    assert record == WakeReport(
        _utc(15, 14, 30, 23), sleep_time=_utc(15, 6, 0, 5.61), wake_time=_utc(15, 14, 30, 21.57)
    )


def test_kernel_general_1_fixture_parses_old_and_new_time():
    record = parse_system((_FIXTURES / "kernel-general-1.xml").read_text(encoding="utf-8"))
    assert record == ClockChange(
        _utc(15, 14, 30, 19.5), old_time=_utc(15, 6, 0, 26.019), new_time=_utc(15, 14, 30, 19.5), reason=2
    )


@pytest.mark.parametrize(
    ("name", "kind", "provider", "event_id"),
    [
        ("eventlog-6005.xml", BootKind.START, "EventLog", 6005),
        ("eventlog-6006.xml", BootKind.STOP, "EventLog", 6006),
        ("kernel-general-12.xml", BootKind.START, "Microsoft-Windows-Kernel-General", 12),
        ("kernel-general-13.xml", BootKind.STOP, "Microsoft-Windows-Kernel-General", 13),
    ],
)
def test_boot_fixtures_parse_as_boot_records(name, kind, provider, event_id):
    record = parse_system((_FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(record, BootRecord)
    assert (record.kind, record.provider, record.event_id) == (kind, provider, event_id)


@pytest.mark.parametrize("event_id", [107, 566, 41, 172])
def test_kernel_power_records_outside_the_confirmed_set_are_not_parsed(event_id):
    assert parse_system(_fixture("kernel-power-506.xml", event_id=event_id)) is None


def test_a_winlogon_record_is_not_a_system_record():
    assert parse_system(_winlogon(4)) is None


@pytest.mark.parametrize(
    ("xml", "message"),
    [
        ("<Event", "not well-formed"),
        ("<Event xmlns='http://schemas.microsoft.com/win/2004/08/events/event'/>", "no System"),
        (re.sub(r"<TimeCreated[^>]*/>", "", _entry(_utc(14, 8, 0))), "TimeCreated is missing"),
        (_entry(_utc(14, 8, 0)).replace("0Z'", "0'"), "TimeCreated is not UTC"),
        (_entry(_utc(14, 8, 0), reason="twelve"), "Reason is not an integer"),
        (re.sub(r"<Data Name='Reason'>[^<]*</Data>", "", _entry(_utc(14, 8, 0))), "Reason is not an integer"),
        (_fixture("power-troubleshooter-1.xml", WakeTime="yesterday"), "WakeTime is not ISO 8601"),
        (_fixture("kernel-power-42.xml", TargetState=""), "TargetState is not an integer"),
    ],
)
def test_a_malformed_confirmed_record_raises(xml, message):
    with pytest.raises(EventLogFormatError, match=message):
        parse_system(xml)


# Winlogon/Operational 811 Sens: the code mapping, pinned (section 5).


@pytest.mark.parametrize(("code", "event"), [(4, LOCK), (3, LOCK), (5, UNLOCK), (2, UNLOCK)])
def test_sens_codes_map_to_lock_and_unlock(code, event):
    ts = _utc(15, 6, 2, 30.468)
    assert parse_winlogon(_winlogon(code, ts=ts), _USER) == SessionRecord(ts, event, code)


def test_the_811_fixture_is_an_unlock_with_its_channel_and_code_in_detail():
    record = parse_winlogon((_FIXTURES / "winlogon-811.xml").read_text(encoding="utf-8"), _USER)
    assert record == SessionRecord(_utc(15, 6, 2, 30.468), UNLOCK, 5)
    assert record.detail == "Winlogon/Operational 811 Sens 5"


def test_detail_matches_what_replay_deduplicates_on():
    assert parse_winlogon(_winlogon(4), _USER).detail == "Winlogon/Operational 811 Sens 4"


@pytest.mark.parametrize("code", [0, 1, 12, 13, 14])
def test_other_sens_codes_are_neither_lock_nor_unlock(code):
    assert parse_winlogon(_winlogon(code), _USER) is None


@pytest.mark.parametrize("subscriber", ["TermSrv", "GPClient", "Profiles", "Wlansvc", "sens"])
@pytest.mark.parametrize("code", [2, 3, 4, 5])
def test_other_subscribers_are_ignored_whatever_the_code(subscriber, code):
    assert parse_winlogon(_winlogon(code, subscriber=subscriber), _USER) is None


@pytest.mark.parametrize("user", [_OTHER_USER, _SYSTEM_SID])
def test_another_users_records_are_ignored(user):
    assert parse_winlogon(_winlogon(4, user=user), _USER) is None


def test_a_record_with_no_user_is_ignored():
    xml = re.sub(r"<Security[^>]*/>", "<Security/>", _winlogon(4))
    assert parse_winlogon(xml, _USER) is None


def test_the_sid_compares_case_insensitively():
    assert parse_winlogon(_winlogon(4, user=_USER.lower()), _USER) is not None


def test_a_system_record_is_not_a_winlogon_record():
    assert parse_winlogon(_entry(_utc(14, 8, 0)), _USER) is None


def test_another_winlogon_event_id_is_ignored():
    assert parse_winlogon(_fixture("winlogon-811.xml", event_id=812), _USER) is None


def test_a_malformed_811_code_raises():
    with pytest.raises(EventLogFormatError, match="Event is not an integer"):
        parse_winlogon(_winlogon("lock"), _USER)


# XPath queries.


def test_system_xpath_names_every_confirmed_record_and_nothing_else():
    xpath = system_xpath(_utc(15, 6, 0))
    assert (
        "Provider[@Name='Microsoft-Windows-Kernel-Power'] and (EventID=506 or EventID=507 or EventID=42)"
        in xpath
    )
    assert "Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and (EventID=1)" in xpath
    assert (
        "Provider[@Name='Microsoft-Windows-Kernel-General'] and (EventID=1 or EventID=12 or EventID=13)"
        in xpath
    )
    assert "Provider[@Name='EventLog'] and (EventID=6005 or EventID=6006)" in xpath
    assert "107" not in xpath
    assert "566" not in xpath


def test_the_since_filter_is_utc_truncated_to_the_millisecond():
    since = datetime(2026, 9, 15, 8, 0, 0, 123999, tzinfo=ZoneInfo("Europe/Copenhagen"))
    assert "TimeCreated[@SystemTime>='2026-09-15T06:00:00.123Z']" in system_xpath(since)
    assert "TimeCreated[@SystemTime>='2026-09-15T06:00:00.123Z']" in winlogon_xpath(since)


def test_winlogon_xpath_asks_for_811_only():
    assert winlogon_xpath(_utc(15, 6, 0)).startswith(
        "*[System[Provider[@Name='Microsoft-Windows-Winlogon'] and EventID=811 and "
    )


def test_a_naive_since_is_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        system_xpath(datetime(2026, 9, 15, 6, 0))  # noqa: DTZ001


# Boundaries: chains collapsed, hibernate wakes, boots.


def test_a_plain_standby_is_one_suspend_and_one_resume():
    result = _at(_entry(_utc(15, 10, 0), 12), _exit(_utc(15, 10, 40), 31), until=_utc(15, 11, 0))
    assert result == [
        Boundary(_utc(15, 10, 0), SUSPEND, "System Kernel-Power 506 reason 12"),
        Boundary(_utc(15, 10, 40), RESUME, "System Kernel-Power 507 reason 31", reason=31),
    ]


def test_a_chain_collapses_to_the_first_506_and_the_terminal_507():
    # Lid closed (screen off, display burst, lid), an Austerity chain step overnight, a mouse wake in the morning.
    result = _at(
        _entry(_utc(14, 14, 32, 7), 11),
        _exit(_utc(14, 14, 32, 8), 28),
        _entry(_utc(14, 14, 32, 9), 28),
        _exit(_utc(14, 14, 32, 10), 15),
        _entry(_utc(14, 14, 32, 11), 15),
        _exit(_utc(14, 15, 14, 50), 55),
        _entry(_utc(14, 15, 14, 50.8), 55),
        _exit(_utc(15, 6, 33, 21), 32),
        until=_utc(15, 7, 0),
    )
    assert result == [
        Boundary(_utc(14, 14, 32, 7), SUSPEND, "System Kernel-Power 506 reason 11"),
        Boundary(_utc(15, 6, 33, 21), RESUME, "System Kernel-Power 507 reason 32", reason=32),
    ]


def test_a_506_exactly_two_seconds_after_a_507_is_a_chain_step():
    start, exit_ = _utc(15, 10, 0), _utc(15, 11, 0)
    result = _at(
        _entry(start),
        _exit(exit_, 55),
        _entry(exit_ + CHAIN_STEP, 55),
        _exit(_utc(15, 12, 0)),
        until=_utc(15, 13, 0),
    )
    assert [boundary.time for boundary in result] == [start, _utc(15, 12, 0)]


def test_a_506_just_over_two_seconds_after_a_507_is_a_new_sleep():
    exit_ = _utc(15, 11, 0)
    later = exit_ + CHAIN_STEP + timedelta(milliseconds=1)
    result = _at(
        _entry(_utc(15, 10, 0)), _exit(exit_), _entry(later), _exit(_utc(15, 12, 0)), until=_utc(15, 13, 0)
    )
    assert [(boundary.event, boundary.time) for boundary in result] == [
        (SUSPEND, _utc(15, 10, 0)),
        (RESUME, exit_),
        (SUSPEND, later),
        (RESUME, _utc(15, 12, 0)),
    ]


def test_a_507_younger_than_the_chain_step_waits_for_the_next_read():
    exit_ = _utc(15, 11, 0)
    xmls = (_entry(_utc(15, 10, 0)), _exit(exit_))
    assert _shape(_at(*xmls, until=exit_ + timedelta(seconds=1))) == [
        (SUSPEND, _utc(15, 10, 0), "System Kernel-Power 506 reason 12")
    ]
    assert [boundary.event for boundary in _at(*xmls, until=exit_ + CHAIN_STEP)] == [SUSPEND, RESUME]


def test_a_507_with_no_506_before_it_in_the_window_still_resumes():
    assert [boundary.event for boundary in _at(_exit(_utc(15, 8, 0)), until=_utc(15, 9, 0))] == [RESUME]


def test_a_second_506_while_asleep_adds_nothing():
    result = _at(_entry(_utc(15, 10, 0), 11), _entry(_utc(15, 10, 5), 15), until=_utc(15, 11, 0))
    assert _shape(result) == [(SUSPEND, _utc(15, 10, 0), "System Kernel-Power 506 reason 11")]


def _26_september(*, wake_report: bool) -> list[str]:
    """The 26 September shape, synthetic: lid closed on the 25th, a dark wake (507 reason 20) into hibernation, the 107 stamped with the frozen clock, and the real wake in the afternoon, which passes through Modern Standby (506 reason 20, 507 reason 15) between the clock change and the Power-Troubleshooter 1."""
    dark_wake = _utc(26, 5, 50, 45.616)
    xmls = [
        _entry(_utc(25, 14, 36, 21), 11),
        _exit(_utc(25, 14, 36, 22), 28),
        _entry(_utc(25, 14, 36, 23), 15),
        _exit(_utc(25, 15, 14, 50), 55),
        _entry(_utc(25, 15, 14, 50.4), 55),
        _exit(dark_wake, 20),
        _hibernate(_utc(26, 5, 50, 51.744)),
        # Kernel-General 1 Reason 2 at hibernation entry: a 1.289 s correction, not a wake.
        _clock(_utc(26, 5, 51, 1), old=_utc(26, 5, 50, 59.711), new=_utc(26, 5, 51, 1)),
        # Kernel-Power 107, stamped with the clock frozen at hibernation entry: never parsed.
        _fixture("kernel-power-506.xml", ts=_utc(26, 5, 51, 6.019), event_id=107),
        _clock(_utc(26, 14, 30, 19.5), old=_utc(26, 5, 51, 6.019), new=_utc(26, 14, 30, 19.5)),
        _entry(_utc(26, 14, 30, 21.4), 20),
        _exit(_utc(26, 14, 30, 21.6), 15),
    ]
    if wake_report:
        xmls.append(_wake_report(_utc(26, 14, 30, 23), sleep=dark_wake, wake=_utc(26, 14, 30, 21.57)))
    return xmls


def test_the_26_september_shape_resumes_at_the_power_troubleshooter_wake_time():
    result = _at(*_26_september(wake_report=True), until=_utc(26, 15, 0))
    assert result == [
        Boundary(_utc(25, 14, 36, 21), SUSPEND, "System Kernel-Power 506 reason 11"),
        Boundary(_utc(26, 5, 50, 45.616), RESUME, "System Kernel-Power 507 reason 20", reason=20),
        Boundary(_utc(26, 5, 50, 51.744), SUSPEND, "System Kernel-Power 42 reason 13"),
        Boundary(_utc(26, 14, 30, 21.57), RESUME, "System Power-Troubleshooter 1 WakeTime"),
    ]


def test_the_107_is_never_a_boundary():
    times = {boundary.time for boundary in _at(*_26_september(wake_report=True), until=_utc(26, 15, 0))}
    assert _utc(26, 5, 51, 6.019) not in times


def test_a_hibernate_wake_without_power_troubleshooter_resumes_at_the_kernel_general_new_time():
    result = _at(*_26_september(wake_report=False), until=_utc(26, 15, 0))
    assert result[-1] == Boundary(_utc(26, 14, 30, 19.5), RESUME, "System Kernel-General 1 reason 2 NewTime")
    assert len(result) == 4


@pytest.mark.parametrize("wake_report", [True, False])
def test_the_standby_pass_on_the_way_out_of_hibernation_gives_no_boundary(wake_report):
    result = _at(*_26_september(wake_report=wake_report), until=_utc(26, 15, 0))
    assert not [boundary for boundary in result if boundary.time > _utc(26, 14, 30, 21.57)]
    assert [boundary.event for boundary in result] == [SUSPEND, RESUME, SUSPEND, RESUME]


def test_a_sleep_after_the_absorbed_pass_is_a_sleep_again():
    xmls = [*_26_september(wake_report=True), _entry(_utc(26, 15, 10), 12), _exit(_utc(26, 15, 40), 31)]
    result = _at(*xmls, until=_utc(26, 16, 0))
    assert _shape(result)[-2:] == [
        (SUSPEND, _utc(26, 15, 10), "System Kernel-Power 506 reason 12"),
        (RESUME, _utc(26, 15, 40), "System Kernel-Power 507 reason 31"),
    ]


def test_a_kernel_general_wake_waits_while_a_power_troubleshooter_may_still_come():
    new_time = _utc(26, 14, 30, 19.5)
    xmls = _26_september(wake_report=False)
    assert [boundary.event for boundary in _at(*xmls, until=new_time + timedelta(seconds=5))] == [
        SUSPEND,
        RESUME,
        SUSPEND,
    ]
    assert len(_at(*xmls, until=new_time + WAKE_REPORT_LAG)) == 4


def test_a_large_clock_change_for_another_reason_is_not_a_wake():
    result = _at(
        _hibernate(_utc(15, 6, 0)),
        _clock(_utc(15, 14, 0), old=_utc(15, 6, 0, 10), new=_utc(15, 14, 0), reason=1),
        until=_utc(15, 15, 0),
    )
    assert _shape(result) == [(SUSPEND, _utc(15, 6, 0), "System Kernel-Power 42 reason 13")]


def test_a_large_clock_change_while_awake_is_not_a_wake():
    assert _at(_clock(_utc(15, 14, 0), old=_utc(15, 6, 0), new=_utc(15, 14, 0)), until=_utc(15, 15, 0)) == []


def test_a_wake_report_while_awake_is_not_a_boundary():
    report = _wake_report(_utc(15, 14, 0), sleep=_utc(15, 6, 0), wake=_utc(15, 13, 59, 58))
    assert _at(report, until=_utc(15, 15, 0)) == []


def test_a_42_while_already_asleep_adds_no_second_suspend():
    result = _at(
        _entry(_utc(15, 6, 0), 12),
        _hibernate(_utc(15, 8, 0)),
        _wake_report(_utc(15, 14, 0, 2), sleep=_utc(15, 8, 0), wake=_utc(15, 14, 0)),
        until=_utc(15, 15, 0),
    )
    assert _shape(result) == [
        (SUSPEND, _utc(15, 6, 0), "System Kernel-Power 506 reason 12"),
        (RESUME, _utc(15, 14, 0), "System Power-Troubleshooter 1 WakeTime"),
    ]


def test_a_507_ends_a_hibernation_the_log_never_reports_a_wake_for():
    result = _at(_hibernate(_utc(15, 6, 0)), _exit(_utc(15, 6, 30), 1), until=_utc(15, 7, 0))
    assert [boundary.event for boundary in result] == [SUSPEND, RESUME]
    assert result[1].reason == 1


def test_boot_records_become_boot_boundaries_and_end_a_sleep():
    result = _at(
        _entry(_utc(14, 6, 50), 12),
        _fixture("eventlog-6006.xml", ts=_utc(14, 6, 57, 39)),
        _fixture("kernel-general-13.xml", ts=_utc(14, 6, 57, 40.1)),
        _fixture("kernel-general-12.xml", ts=_utc(14, 6, 58, 4)),
        _fixture("eventlog-6005.xml", ts=_utc(14, 6, 58, 10)),
        _entry(_utc(14, 9, 0), 12),
        until=_utc(14, 10, 0),
    )
    assert _shape(result) == [
        (SUSPEND, _utc(14, 6, 50), "System Kernel-Power 506 reason 12"),
        (BOOT, _utc(14, 6, 57, 39), "System EventLog 6006"),
        (BOOT, _utc(14, 6, 57, 40.1), "System Kernel-General 13"),
        (BOOT, _utc(14, 6, 58, 4), "System Kernel-General 12"),
        (BOOT, _utc(14, 6, 58, 10), "System EventLog 6005"),
        (SUSPEND, _utc(14, 9, 0), "System Kernel-Power 506 reason 12"),
    ]
    assert [boundary.boot_kind for boundary in result] == [
        None,
        BootKind.STOP,
        BootKind.STOP,
        BootKind.START,
        BootKind.START,
        None,
    ]


def test_record_order_does_not_matter():
    records = _records(*_26_september(wake_report=True))
    until = _utc(26, 15, 0)
    assert boundaries(reversed(records), until=until) == boundaries(records, until=until)


def test_dst_end_moves_no_boundary():
    # 25 October 2026: 02:10 CEST (00:10 UTC) to 02:50 CET (01:50 UTC), across the repeated 02:00 to 03:00 hour.
    entry, exit_ = _utc(25, 0, 10, month=10), _utc(25, 1, 50, month=10)
    result = _at(_entry(entry), _exit(exit_), until=_utc(25, 2, 0, month=10))
    assert [boundary.time for boundary in result] == [entry, exit_]
    assert result[1].time - result[0].time == timedelta(hours=1, minutes=40)
    local = [boundary.time.astimezone(ZoneInfo("Europe/Copenhagen")) for boundary in result]
    assert [(moment.hour, moment.minute, moment.utcoffset()) for moment in local] == [
        (2, 10, timedelta(hours=2)),
        (2, 50, timedelta(hours=1)),
    ]
