"""The event log backend registry and the reader that joins the I/O seams to the parser. Every seam is a fake: no test touches the real event log."""

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from timeexisting.ledger.events import EventType
from timeexisting.presence import backend
from timeexisting.presence.backend import ChannelCheck, EventLog, NoEventLog, event_log
from timeexisting.presence.eventlog import (
    SYSTEM_CHANNEL,
    WINLOGON_CHANNEL,
    Boundary,
    EventLogUnavailable,
    SessionRecord,
    system_xpath,
    winlogon_xpath,
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "eventlog"
_USER = "S-1-5-21-1000000001-1000000002-1000000003-1001"
_SINCE = datetime(2026, 9, 14, 0, 0, tzinfo=UTC)
_UNTIL = datetime(2026, 9, 16, 0, 0, tzinfo=UTC)


def _xml(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


class _FakeLog:
    """The three seams, answering from canned XML per channel. A channel listed in `unreadable` raises on probe and query."""

    def __init__(
        self, records: dict[str, list[str]], unreadable: tuple[str, ...] = (), sid: str | None = _USER
    ):
        self.records = records
        self.unreadable = unreadable
        self.sid = sid
        self.queries: list[tuple[str, str]] = []
        self.probes: list[str] = []

    def query(self, channel: str, xpath: str) -> list[str]:
        self.queries.append((channel, xpath))
        if channel in self.unreadable:
            raise EventLogUnavailable(f"{channel}: EvtQuery failed, 5 Access is denied.")
        return list(self.records.get(channel, []))

    def probe(self, channel: str) -> None:
        self.probes.append(channel)
        if channel in self.unreadable:
            raise EventLogUnavailable(f"{channel}: EvtQuery failed, 5 Access is denied.")

    def user_sid(self) -> str:
        if self.sid is None:
            raise EventLogUnavailable("user SID: OpenProcessToken failed, 5 Access is denied.")
        return self.sid

    def reader(self) -> EventLog:
        return EventLog(self.query, self.probe, self.user_sid)


def test_system_boundaries_query_the_system_channel_and_parse_the_records():
    fake = _FakeLog({SYSTEM_CHANNEL: [_xml("kernel-power-506.xml"), _xml("kernel-power-507.xml")]})
    result = fake.reader().system_boundaries(_SINCE, _UNTIL)
    assert fake.queries == [(SYSTEM_CHANNEL, system_xpath(_SINCE))]
    assert [(boundary.event, boundary.detail) for boundary in result] == [
        (EventType.SUSPEND, "System Kernel-Power 506 reason 12"),
        (EventType.RESUME, "System Kernel-Power 507 reason 32"),
    ]


def test_a_failed_system_query_is_logged_and_returns_nothing(caplog):
    fake = _FakeLog({}, unreadable=(SYSTEM_CHANNEL,))
    with caplog.at_level(logging.WARNING, logger=backend.__name__):
        assert fake.reader().system_boundaries(_SINCE, _UNTIL) == []
    assert "event log query failed" in caplog.text


def test_a_malformed_system_record_is_skipped_and_logged(caplog):
    fake = _FakeLog({SYSTEM_CHANNEL: ["<Event", _xml("kernel-power-506.xml")]})
    with caplog.at_level(logging.WARNING, logger=backend.__name__):
        result = fake.reader().system_boundaries(_SINCE, _UNTIL)
    assert [boundary.event for boundary in result] == [EventType.SUSPEND]
    assert "malformed System record" in caplog.text


def test_session_records_query_winlogon_and_keep_this_users_sens_records_in_time_order():
    unlock = _xml("winlogon-811.xml")
    lock = unlock.replace("<Data Name='Event'>5</Data>", "<Data Name='Event'>4</Data>").replace(
        "2026-09-15T06:02:30.4680000Z", "2026-09-15T06:01:00.0000000Z"
    )
    other_user = unlock.replace(_USER, "S-1-5-18")
    other_subscriber = unlock.replace(">Sens<", ">TermSrv<")
    fake = _FakeLog({WINLOGON_CHANNEL: [unlock, other_user, other_subscriber, lock]})
    result = fake.reader().session_records(_SINCE)
    assert fake.queries == [(WINLOGON_CHANNEL, winlogon_xpath(_SINCE))]
    assert result == [
        SessionRecord(datetime(2026, 9, 15, 6, 1, tzinfo=UTC), EventType.LOCK, 4),
        SessionRecord(datetime(2026, 9, 15, 6, 2, 30, 468000, tzinfo=UTC), EventType.UNLOCK, 5),
    ]


def test_no_user_sid_reads_no_winlogon_records(caplog):
    fake = _FakeLog({WINLOGON_CHANNEL: [_xml("winlogon-811.xml")]}, sid=None)
    with caplog.at_level(logging.WARNING, logger=backend.__name__):
        assert fake.reader().session_records(_SINCE) == []
    assert fake.queries == []
    assert "no user SID" in caplog.text


def test_a_failed_winlogon_query_returns_nothing():
    fake = _FakeLog({}, unreadable=(WINLOGON_CHANNEL,))
    assert fake.reader().session_records(_SINCE) == []


def test_a_malformed_winlogon_record_is_skipped_and_logged(caplog):
    broken = _xml("winlogon-811.xml").replace("<Data Name='Event'>5</Data>", "<Data Name='Event'>x</Data>")
    fake = _FakeLog({WINLOGON_CHANNEL: [broken, _xml("winlogon-811.xml")]})
    with caplog.at_level(logging.WARNING, logger=backend.__name__):
        assert len(fake.reader().session_records(_SINCE)) == 1
    assert "malformed Winlogon record" in caplog.text


@pytest.mark.parametrize(
    ("unreadable", "expected"),
    [
        ((), ChannelCheck(system=True, winlogon=True)),
        ((WINLOGON_CHANNEL,), ChannelCheck(system=True, winlogon=False)),
        ((SYSTEM_CHANNEL,), ChannelCheck(system=False, winlogon=True)),
        ((SYSTEM_CHANNEL, WINLOGON_CHANNEL), ChannelCheck(system=False, winlogon=False)),
    ],
)
def test_check_probes_each_channel_once(unreadable, expected):
    fake = _FakeLog({}, unreadable=unreadable)
    assert fake.reader().check() == expected
    assert fake.probes == [SYSTEM_CHANNEL, WINLOGON_CHANNEL]
    assert fake.queries == []


def test_check_reads_no_records():
    fake = _FakeLog({WINLOGON_CHANNEL: [_xml("winlogon-811.xml")]})
    fake.reader().check()
    assert fake.queries == []


def test_no_event_log_checks_nothing_and_returns_nothing():
    reader = NoEventLog()
    assert reader.check() is None
    assert reader.system_boundaries(_SINCE, _UNTIL) == []
    assert reader.session_records(_SINCE) == []


@pytest.mark.parametrize("platform", ["linux", "darwin", "android"])
def test_a_platform_without_a_backend_gets_no_event_log(platform):
    assert isinstance(event_log(platform), NoEventLog)


def test_a_backend_that_cannot_load_falls_back_to_no_event_log(monkeypatch, caplog):
    def broken():
        raise ImportError("No module named 'win32evtlog'")

    monkeypatch.setitem(backend._REGISTRY, "win32", broken)
    with caplog.at_level(logging.WARNING, logger=backend.__name__):
        assert isinstance(event_log("win32"), NoEventLog)
    assert "unavailable" in caplog.text


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows backend imports pywin32")
def test_windows_gets_the_event_log_reader_without_touching_the_log():
    assert isinstance(event_log("win32"), EventLog)


def test_boundary_and_session_types_are_what_the_reader_returns():
    fake = _FakeLog(
        {SYSTEM_CHANNEL: [_xml("kernel-power-42.xml")], WINLOGON_CHANNEL: [_xml("winlogon-811.xml")]}
    )
    reader = fake.reader()
    assert all(isinstance(item, Boundary) for item in reader.system_boundaries(_SINCE, _UNTIL))
    assert all(isinstance(item, SessionRecord) for item in reader.session_records(_SINCE))
