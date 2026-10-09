import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from timeexisting.ledger.events import (
    Confidence,
    Event,
    EventType,
    LedgerFormatError,
    NoteKind,
    Source,
    from_json,
    profile_for,
    to_json,
)

_TS = datetime(2026, 9, 17, 6, 7, 31, tzinfo=UTC)


def _event(**overrides) -> Event:
    fields = {
        "ts": _TS,
        "host": "TEST-HOST",
        "profile": "work",
        "event": EventType.UNLOCK,
        "source": Source.WIN32,
    }
    return Event.new(**(fields | overrides))


def _record(**overrides) -> dict:
    return json.loads(to_json(_event())) | overrides


def test_enums_carry_every_value_from_the_spec():
    assert {member.value for member in EventType} == {
        "collector_start",
        "collector_stop",
        "lock",
        "unlock",
        "suspend",
        "resume",
        "boot",
        "input",
        "day_start",
        "classify",
        "note",
        "dayflag",
        "manual_start",
        "manual_end",
        "void",
    }
    assert {member.value for member in Source} == {
        "win32",
        "wts",
        "probe",
        "power",
        "eventlog",
        "clock",
        "logon",
        "user",
        "dbus",
        "collector",
        "manual",
        "backfill",
    }
    assert {member.value for member in Confidence} == {"observed", "inferred"}
    assert {member.value for member in NoteKind} == {"coverage", "wts_only"}


@pytest.mark.parametrize("key", ["event", "source"])
def test_heartbeat_is_retired_and_rejected_on_read(key):
    assert "heartbeat" not in {member.value for member in EventType}
    assert "heartbeat" not in {member.value for member in Source}
    with pytest.raises(LedgerFormatError, match=f"unknown {key}"):
        from_json(json.dumps(_record(**{key: "heartbeat"})))


def test_new_fills_version_and_id():
    event = _event()
    assert event.v == 1
    assert len(event.id) == 32
    assert event.id == event.id.lower()
    assert event.confidence is Confidence.OBSERVED
    assert event.data == {}


@pytest.mark.parametrize("event_type", list(EventType))
def test_round_trip_every_field(event_type):
    event = _event(
        event=event_type,
        source=Source.BACKFILL,
        confidence=Confidence.INFERRED,
        data={"reason": "signal", "note": "æøå, not escaped"},
    )
    assert from_json(to_json(event)) == event


@pytest.mark.parametrize("source", list(Source))
def test_round_trip_every_source(source):
    event = _event(source=source)
    assert from_json(to_json(event)) == event


@pytest.mark.parametrize(
    ("event_type", "source", "confidence", "data"),
    [
        (EventType.LOCK, Source.WTS, Confidence.OBSERVED, {}),
        (EventType.UNLOCK, Source.WTS, Confidence.OBSERVED, {}),
        (EventType.LOCK, Source.EVENTLOG, Confidence.OBSERVED, {"detail": "Winlogon/Operational 811 Sens 4"}),
        (EventType.LOCK, Source.EVENTLOG, Confidence.OBSERVED, {"detail": "Winlogon/Operational 811 Sens 3"}),
        (
            EventType.UNLOCK,
            Source.EVENTLOG,
            Confidence.OBSERVED,
            {"detail": "Winlogon/Operational 811 Sens 5"},
        ),
        (
            EventType.UNLOCK,
            Source.EVENTLOG,
            Confidence.OBSERVED,
            {"detail": "Winlogon/Operational 811 Sens 2"},
        ),
        (EventType.LOCK, Source.PROBE, Confidence.INFERRED, {}),
        (EventType.UNLOCK, Source.PROBE, Confidence.INFERRED, {}),
        (EventType.UNLOCK, Source.LOGON, Confidence.OBSERVED, {}),
        (EventType.SUSPEND, Source.POWER, Confidence.OBSERVED, {"detail": "PBT_APMSUSPEND"}),
        (EventType.RESUME, Source.POWER, Confidence.OBSERVED, {"detail": "PBT_APMRESUMEAUTOMATIC"}),
        (EventType.SUSPEND, Source.EVENTLOG, Confidence.OBSERVED, {"detail": "Idle Timeout"}),
        (EventType.RESUME, Source.EVENTLOG, Confidence.OBSERVED, {"detail": "Input Mouse"}),
        (EventType.SUSPEND, Source.CLOCK, Confidence.INFERRED, {}),
        (EventType.RESUME, Source.CLOCK, Confidence.INFERRED, {}),
        (EventType.SUSPEND, Source.EVENTLOG, Confidence.OBSERVED, {}),
        (EventType.BOOT, Source.EVENTLOG, Confidence.OBSERVED, {"detail": "EventLog 6005"}),
        (EventType.BOOT, Source.EVENTLOG, Confidence.OBSERVED, {"detail": "Kernel-General 13"}),
        (EventType.BOOT, Source.COLLECTOR, Confidence.INFERRED, {"detail": "boot_time"}),
        (EventType.INPUT, Source.EVENTLOG, Confidence.OBSERVED, {"detail": "Input Keyboard"}),
        (EventType.INPUT, Source.WIN32, Confidence.OBSERVED, {}),
        (
            EventType.DAY_START,
            Source.USER,
            Confidence.OBSERVED,
            {"start": "2026-10-02T06:55:00+00:00", "detail": "prompt"},
        ),
        (
            EventType.DAY_START,
            Source.USER,
            Confidence.OBSERVED,
            {"start": "2026-10-02T06:55:00+00:00", "detail": "flag"},
        ),
        (EventType.NOTE, Source.COLLECTOR, Confidence.OBSERVED, {"kind": "wts_only"}),
    ],
)
def test_round_trip_every_phase_3_shape(event_type, source, confidence, data):
    event = _event(event=event_type, source=source, confidence=confidence, data=data)
    line = to_json(event)
    assert json.loads(line)["event"] == event_type.value
    assert json.loads(line)["source"] == source.value
    assert from_json(line) == event


def test_round_trip_keeps_microseconds():
    event = _event(ts=_TS.replace(microsecond=123456))
    assert from_json(to_json(event)).ts == event.ts


def test_matches_the_spec_example_line():
    line = (
        '{"v": 1, "id": "01a0adfa34b87b2c9e41d5a6f0b3c872", "ts": "2026-09-17T06:07:31+00:00", '
        '"host": "TEST-HOST", "profile": "work", "event": "unlock", "source": "win32", "confidence": "observed"}'
    )
    assert to_json(from_json(line)) == line


def test_keys_are_in_a_fixed_order():
    event = _event(data={"reason": "stop_file"})
    assert list(json.loads(to_json(event))) == [
        "v",
        "id",
        "ts",
        "host",
        "profile",
        "event",
        "source",
        "confidence",
        "data",
    ]


def test_empty_data_is_omitted():
    assert "data" not in json.loads(to_json(_event()))


def test_empty_data_round_trips():
    # The writer omits an empty `data`; the reader must treat the key as optional, or the two disagree.
    event = _event()
    line = to_json(event)
    assert "data" not in json.loads(line)
    restored = from_json(line)
    assert restored == event
    assert restored.data == {}


def test_explicit_empty_data_is_accepted():
    event = from_json(json.dumps(_record(data={})))
    assert event.data == {}
    assert "data" not in json.loads(to_json(event))


def test_one_line_no_trailing_newline():
    line = to_json(_event(data={"note": "two\nlines"}))
    assert "\n" not in line
    assert not line.endswith(("\n", "\r"))


def test_ts_written_as_iso_8601_utc_with_offset():
    assert json.loads(to_json(_event()))["ts"] == "2026-09-17T06:07:31+00:00"


def test_aware_non_utc_ts_is_converted_to_utc():
    local = _TS.astimezone(ZoneInfo("Europe/Copenhagen"))
    event = _event(ts=local)
    assert event.ts.tzinfo is UTC
    assert event.ts == _TS
    offset = datetime(2026, 9, 17, 8, 7, 31, tzinfo=timezone(timedelta(hours=2))).isoformat()
    assert from_json(json.dumps(_record(ts=offset))).ts == _TS


def test_naive_ts_rejected_on_construction():
    with pytest.raises(LedgerFormatError, match="timezone-aware"):
        _event(ts=datetime(2026, 9, 17, 6, 7, 31))  # noqa: DTZ001 -- the naive value under test


def test_naive_ts_rejected_on_read():
    with pytest.raises(LedgerFormatError, match="timezone-aware"):
        from_json(json.dumps(_record(ts="2026-09-17T06:07:31")))


@pytest.mark.parametrize("version", [0, 2, "1", 1.0, True, None])
def test_unknown_version_rejected(version):
    with pytest.raises(LedgerFormatError, match="schema version"):
        from_json(json.dumps(_record(v=version)))


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("not json", "not valid JSON"),
        ("[1, 2]", "not a JSON object"),
        ("", "not valid JSON"),
        (json.dumps({"v": 1}), "missing keys"),
        (json.dumps(_record(extra="x")), "unknown keys: extra"),
        (json.dumps(_record(ts="yesterday")), "not ISO 8601"),
        (json.dumps(_record(ts=1758089251)), "ISO 8601 string"),
        (json.dumps(_record(id="ABC")), "32 lowercase hex"),
        (json.dumps(_record(id="01A0ADFA34B87B2C9E41D5A6F0B3C872")), "32 lowercase hex"),
        (json.dumps(_record(host="")), "host must be"),
        (json.dumps(_record(event="teleport")), "unknown event"),
        (json.dumps(_record(source="psychic")), "unknown source"),
        (json.dumps(_record(confidence="certain")), "unknown confidence"),
        (json.dumps(_record(data=["reason"])), "data must map"),
        (json.dumps(_record(data={"count": 3})), "data must map"),
    ],
)
def test_malformed_lines_rejected(line, message):
    with pytest.raises(LedgerFormatError, match=message):
        from_json(line)


def test_event_is_frozen_and_data_is_read_only():
    event = _event(data={"reason": "signal"})
    with pytest.raises(AttributeError):
        event.host = "elsewhere"  # type: ignore[misc]
    with pytest.raises(TypeError):
        event.data["reason"] = "tampered"  # type: ignore[index]


def test_data_is_copied_not_shared():
    source = {"reason": "signal"}
    event = _event(data=source)
    source["reason"] = "tampered"
    assert event.data["reason"] == "signal"


def test_events_are_hashable():
    event = _event(data={"reason": "signal"})
    assert event in {event}


def test_ids_are_unique():
    ids = {_event().id for _ in range(10_000)}
    assert len(ids) == 10_000


def test_ids_sort_in_creation_order():
    ids = [_event().id for _ in range(10_000)]
    assert ids == sorted(ids)


def test_profile_for_uses_the_host_map(cfg):
    cfg = replace(cfg, profiles=replace(cfg.profiles, default="fun", hosts={"TEST-HOST": "work"}))
    assert profile_for("TEST-HOST", cfg) == "work"


def test_profile_for_falls_back_to_default(cfg):
    cfg = replace(cfg, profiles=replace(cfg.profiles, default="fun", hosts={"TEST-HOST": "work"}))
    assert profile_for("fedora-box", cfg) == "fun"
