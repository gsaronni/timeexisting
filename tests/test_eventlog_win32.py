"""The thin Windows I/O layer of the event log reader. Every pywin32 call is replaced: no test touches the real event log or the real process token."""

import pytest

win32evtlog = pytest.importorskip("win32evtlog")
pywintypes = pytest.importorskip("pywintypes")

from timeexisting.presence import eventlog_win32  # noqa: E402
from timeexisting.presence.eventlog import EventLogUnavailable  # noqa: E402

_FLAGS = win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryForwardDirection


class _FakeApi:
    def __init__(self, pages: list[list[str]], fail_on: str | None = None) -> None:
        self.pages = list(pages)
        self.fail_on = fail_on
        self.calls: list[tuple] = []

    def _maybe_fail(self, name: str) -> None:
        if self.fail_on == name:
            raise pywintypes.error(5, name, "Access is denied.")

    def evt_query(self, channel, flags, xpath):
        self.calls.append(("EvtQuery", channel, flags, xpath))
        self._maybe_fail("EvtQuery")
        return "handle"

    def evt_next(self, handle, count):
        self.calls.append(("EvtNext", handle, count))
        self._maybe_fail("EvtNext")
        return tuple(self.pages.pop(0)) if self.pages else ()

    def evt_render(self, event, flags):
        assert flags == win32evtlog.EvtRenderEventXml
        return f"<xml>{event}</xml>"


@pytest.fixture
def api(monkeypatch):
    def install(pages: list[list[str]], fail_on: str | None = None) -> _FakeApi:
        fake = _FakeApi(pages, fail_on)
        monkeypatch.setattr(eventlog_win32.win32evtlog, "EvtQuery", fake.evt_query)
        monkeypatch.setattr(eventlog_win32.win32evtlog, "EvtNext", fake.evt_next)
        monkeypatch.setattr(eventlog_win32.win32evtlog, "EvtRender", fake.evt_render)
        return fake

    return install


def test_query_renders_every_page_in_order(api):
    fake = api([["a", "b"], ["c"]])
    assert eventlog_win32.query("System", "*[System]") == ["<xml>a</xml>", "<xml>b</xml>", "<xml>c</xml>"]
    assert fake.calls[0] == ("EvtQuery", "System", _FLAGS, "*[System]")
    assert [call[0] for call in fake.calls[1:]] == ["EvtNext", "EvtNext", "EvtNext"]


def test_query_of_an_empty_result_is_empty(api):
    api([])
    assert eventlog_win32.query("System", "*") == []


@pytest.mark.parametrize("fail_on", ["EvtQuery", "EvtNext"])
def test_a_failing_query_raises_unavailable_with_the_channel(api, fail_on):
    api([["a"]], fail_on=fail_on)
    with pytest.raises(EventLogUnavailable, match=rf"System: {fail_on} failed, 5"):
        eventlog_win32.query("System", "*")


def test_first_reads_one_record_and_renders_it(api):
    fake = api([["a"], ["b"]])
    assert eventlog_win32.first("System", "*[System]") == "<xml>a</xml>"
    assert fake.calls == [("EvtQuery", "System", _FLAGS, "*[System]"), ("EvtNext", "handle", 1)]


def test_first_of_an_empty_result_is_none(api):
    api([])
    assert eventlog_win32.first("System", "*") is None


@pytest.mark.parametrize("fail_on", ["EvtQuery", "EvtNext"])
def test_a_failing_first_raises_unavailable_with_the_channel(api, fail_on):
    api([["a"]], fail_on=fail_on)
    with pytest.raises(EventLogUnavailable, match=rf"System: {fail_on} failed, 5"):
        eventlog_win32.first("System", "*")


def test_probe_opens_a_query_and_reads_nothing(api):
    fake = api([["a"]])
    eventlog_win32.probe("Microsoft-Windows-Winlogon/Operational")
    assert fake.calls == [("EvtQuery", "Microsoft-Windows-Winlogon/Operational", _FLAGS, "*")]


def test_probe_of_an_unreadable_channel_raises_unavailable(api):
    api([], fail_on="EvtQuery")
    with pytest.raises(EventLogUnavailable, match="Security: EvtQuery failed, 5"):
        eventlog_win32.probe("Security")


def test_current_user_sid_reads_the_process_token(monkeypatch):
    security = eventlog_win32.win32security
    monkeypatch.setattr(eventlog_win32.win32api, "GetCurrentProcess", lambda: "process")
    monkeypatch.setattr(security, "OpenProcessToken", lambda process, access: ("token", process, access))
    monkeypatch.setattr(security, "GetTokenInformation", lambda token, kind: (("sid", token, kind), 0))
    monkeypatch.setattr(
        security, "ConvertSidToStringSid", lambda sid: "S-1-5-21-1-2-3-1001" if sid[0] == "sid" else ""
    )
    assert eventlog_win32.current_user_sid() == "S-1-5-21-1-2-3-1001"


def test_current_user_sid_failure_raises_unavailable(monkeypatch):
    def refuse(*_args):
        raise pywintypes.error(5, "OpenProcessToken", "Access is denied.")

    monkeypatch.setattr(eventlog_win32.win32security, "OpenProcessToken", refuse)
    with pytest.raises(EventLogUnavailable, match="user SID: OpenProcessToken failed"):
        eventlog_win32.current_user_sid()
