import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from timeexisting.ledger import store
from timeexisting.ledger.events import Confidence, Event, EventType, Source, from_json, to_json
from timeexisting.ledger.replay import Coverage, Gap, Presence, Timeline, read_coverage, replay

_PHASE_2_SHARD = Path(__file__).parent / "fixtures" / "phase2-shard.jsonl"
_T0 = datetime(2026, 9, 17, 6, 0, 0, tzinfo=UTC)
_CPH = ZoneInfo("Europe/Copenhagen")


def _at(minutes: int) -> datetime:
    return _T0 + timedelta(minutes=minutes)


def _start(ts: datetime, host: str = "laptop", reason: str = "launch", **overrides) -> Event:
    return Event.new(
        ts=ts,
        host=host,
        profile="work",
        event=EventType.COLLECTOR_START,
        source=Source.COLLECTOR,
        data={"reason": reason},
        **overrides,
    )


def _stop(ts: datetime, host: str = "laptop", reason: str = "signal", **overrides) -> Event:
    return Event.new(
        ts=ts,
        host=host,
        profile="work",
        event=EventType.COLLECTOR_STOP,
        source=Source.COLLECTOR,
        data={"reason": reason},
        **overrides,
    )


def test_empty_ledger_gives_an_empty_timeline():
    assert replay([]) == Timeline(events=(), presences=(), gaps=())


def test_start_and_stop_give_one_interval():
    timeline = replay([_start(_at(0)), _stop(_at(60))])
    assert timeline.presences == (Presence(_at(0), _at(60), "laptop", end_inferred=False),)
    assert timeline.gaps == ()


def test_stop_then_start_give_a_gap_carrying_the_stops_reason():
    timeline = replay([_start(_at(0)), _stop(_at(60), reason="stop_file"), _start(_at(90))])
    assert timeline.gaps == (
        Gap(_at(60), _at(90), "laptop", start_inferred=False, end_inferred=False, reason="stop_file"),
    )
    assert timeline.presences == (
        Presence(_at(0), _at(60), "laptop", end_inferred=False),
        Presence(_at(90), None, "laptop", end_inferred=False),
    )


def test_inferred_stop_marks_the_interval_end_and_the_gap_start():
    for reason in ("unclean", "suspended"):
        timeline = replay(
            [
                _start(_at(0)),
                _stop(_at(60), reason=reason, confidence=Confidence.INFERRED),
                _start(_at(75), reason="resumed"),
                _stop(_at(120)),
            ]
        )
        assert timeline.presences[0] == Presence(_at(0), _at(60), "laptop", end_inferred=True)
        assert timeline.gaps == (
            Gap(_at(60), _at(75), "laptop", start_inferred=True, end_inferred=False, reason=reason),
        )


def test_inferred_start_marks_the_gap_end():
    timeline = replay([_start(_at(0)), _stop(_at(60)), _start(_at(75), confidence=Confidence.INFERRED)])
    assert timeline.gaps[0].end_inferred is True


def test_stop_without_a_reason_gives_a_gap_with_no_reason():
    stop = Event.new(
        ts=_at(60), host="laptop", profile="work", event=EventType.COLLECTOR_STOP, source=Source.COLLECTOR
    )
    timeline = replay([_start(_at(0)), stop, _start(_at(90))])
    assert timeline.gaps[0].reason is None


def test_start_start_gives_an_unclosed_interval_and_no_gap():
    timeline = replay([_start(_at(0)), _start(_at(60)), _stop(_at(90))])
    assert timeline.presences == (
        Presence(_at(0), None, "laptop", end_inferred=False),
        Presence(_at(60), _at(90), "laptop", end_inferred=False),
    )
    assert timeline.gaps == ()


def test_a_hosts_final_start_is_open():
    timeline = replay([_start(_at(0)), _stop(_at(30)), _start(_at(45))])
    assert timeline.presences[-1] == Presence(_at(45), None, "laptop", end_inferred=False)


def test_stop_with_no_open_interval_produces_nothing_and_is_kept():
    orphan = _stop(_at(0))
    timeline = replay([orphan, _start(_at(30))])
    assert timeline.presences == (Presence(_at(30), None, "laptop", end_inferred=False),)
    assert timeline.gaps == ()
    assert orphan in timeline.events


def test_a_second_stop_does_not_move_the_gap_start():
    timeline = replay([_start(_at(0)), _stop(_at(30), reason="signal"), _stop(_at(40)), _start(_at(60))])
    assert timeline.gaps == (
        Gap(_at(30), _at(60), "laptop", start_inferred=False, end_inferred=False, reason="signal"),
    )


def test_other_event_types_are_kept_and_ignored_for_pairing():
    note = Event.new(
        ts=_at(10),
        host="laptop",
        profile="work",
        event=EventType.NOTE,
        source=Source.MANUAL,
        data={"text": "hi"},
    )
    lock = Event.new(ts=_at(20), host="laptop", profile="work", event=EventType.LOCK, source=Source.WIN32)
    timeline = replay([_start(_at(0)), note, lock, _stop(_at(30))])
    assert timeline.events[1:3] == (note, lock)
    assert timeline.presences == (Presence(_at(0), _at(30), "laptop", end_inferred=False),)


def test_unsorted_input_gives_the_same_timeline():
    events = [_start(_at(0)), _stop(_at(30)), _start(_at(45)), _stop(_at(90)), _start(_at(120))]
    shuffled = events[:]
    random.Random(7).shuffle(shuffled)
    assert replay(shuffled) == replay(events)
    assert replay(shuffled).events == tuple(events)


def test_equal_timestamps_are_ordered_by_id():
    stop = _stop(_at(30))
    start = _start(_at(30))
    timeline = replay([start, _start(_at(0)), stop])
    assert timeline.events[1:] == (stop, start)
    assert timeline.gaps == (
        Gap(_at(30), _at(30), "laptop", start_inferred=False, end_inferred=False, reason="signal"),
    )


def test_two_hosts_are_kept_separate():
    timeline = replay(
        [
            _start(_at(0), host="laptop"),
            _start(_at(5), host="desktop"),
            _stop(_at(30), host="laptop"),
            _start(_at(40), host="laptop"),
            _stop(_at(50), host="desktop"),
        ]
    )
    assert timeline.presences == (
        Presence(_at(0), _at(30), "laptop", end_inferred=False),
        Presence(_at(5), _at(50), "desktop", end_inferred=False),
        Presence(_at(40), None, "laptop", end_inferred=False),
    )
    assert timeline.gaps == (
        Gap(_at(30), _at(40), "laptop", start_inferred=False, end_inferred=False, reason="signal"),
    )


def test_a_run_crossing_local_midnight_is_one_continuous_interval():
    start = datetime(2026, 9, 17, 23, 30, tzinfo=_CPH)
    stop = datetime(2026, 9, 18, 0, 45, tzinfo=_CPH)
    timeline = replay([_start(start), _stop(stop)])
    assert timeline.presences == (Presence(start.astimezone(UTC), stop.astimezone(UTC), "laptop", False),)


def test_a_run_across_the_autumn_fall_back_stays_ordered_in_utc():
    # 25 October 2026: local 03:00 CEST becomes 02:00 CET, so 02:xx happens twice.
    local = [
        (datetime(2026, 10, 25, 1, 50, tzinfo=_CPH), _start),
        (datetime(2026, 10, 25, 2, 40, fold=0, tzinfo=_CPH), _stop),
        (datetime(2026, 10, 25, 2, 10, fold=1, tzinfo=_CPH), _start),
        (datetime(2026, 10, 25, 2, 50, fold=1, tzinfo=_CPH), _stop),
    ]
    events = [make(ts) for ts, make in local]
    timeline = replay(reversed(events))

    stamps = [event.ts for event in timeline.events]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)
    assert all(ts.tzinfo is UTC for ts in stamps)
    assert [presence.end - presence.start for presence in timeline.presences] == [
        timedelta(minutes=50),
        timedelta(minutes=40),
    ]
    (gap,) = timeline.gaps
    assert gap.end - gap.start == timedelta(minutes=30)


def _note(data: dict[str, str], event: EventType = EventType.NOTE) -> Event:
    return Event.new(
        ts=_at(0), host="laptop", profile="work", event=event, source=Source.COLLECTOR, data=data
    )


_COVERAGE = Coverage(
    span_start=_at(-120),
    span_end=_at(0),
    system_readable=True,
    system_reaches_back=True,
    winlogon_readable=True,
    winlogon_reaches_back=False,
)


def test_coverage_note_round_trips_through_the_ledger_line():
    note = _note(dict(_COVERAGE.data()))
    assert read_coverage(from_json(to_json(note))) == _COVERAGE


def test_coverage_note_writes_the_spec_fields_as_strings():
    assert dict(_COVERAGE.data()) == {
        "kind": "coverage",
        "span_start": "2026-09-17T04:00:00+00:00",
        "span_end": "2026-09-17T06:00:00+00:00",
        "system_readable": "true",
        "system_reaches_back": "true",
        "winlogon_readable": "true",
        "winlogon_reaches_back": "false",
    }


def test_coverage_stamps_in_another_offset_are_read_as_utc():
    data = dict(_COVERAGE.data()) | {"span_start": _at(-120).astimezone(_CPH).isoformat()}
    coverage = read_coverage(_note(data))
    assert coverage == _COVERAGE
    assert coverage.span_start.tzinfo is UTC


def test_coverage_reader_ignores_the_notes_text():
    data = dict(_COVERAGE.data()) | {"text": "system unreadable, nothing reaches back"}
    assert read_coverage(_note(data)) == _COVERAGE


@pytest.mark.parametrize(
    "field",
    [
        "span_start",
        "span_end",
        "system_readable",
        "system_reaches_back",
        "winlogon_readable",
        "winlogon_reaches_back",
    ],
)
def test_coverage_note_with_a_missing_field_is_rejected(field):
    data = dict(_COVERAGE.data())
    del data[field]
    assert read_coverage(_note(data)) is None


@pytest.mark.parametrize(
    "field", ["system_readable", "system_reaches_back", "winlogon_readable", "winlogon_reaches_back"]
)
@pytest.mark.parametrize("value", ["True", "FALSE", "yes", "1", "0", ""])
def test_coverage_note_with_a_non_boolean_field_is_rejected(field, value):
    data = dict(_COVERAGE.data()) | {field: value}
    assert read_coverage(_note(data)) is None


@pytest.mark.parametrize("value", ["2026-09-17T04:00:00", "yesterday", ""])
@pytest.mark.parametrize("field", ["span_start", "span_end"])
def test_coverage_note_with_a_naive_or_unparseable_stamp_is_rejected(field, value):
    data = dict(_COVERAGE.data()) | {field: value}
    assert read_coverage(_note(data)) is None


def test_coverage_note_whose_span_ends_before_it_starts_is_rejected():
    data = dict(_COVERAGE.data()) | {"span_end": _at(-121).isoformat()}
    assert read_coverage(_note(data)) is None


def test_a_note_of_another_kind_is_not_coverage():
    assert read_coverage(_note({"kind": "wts_only"})) is None
    assert read_coverage(_note({"text": "free text"})) is None
    assert read_coverage(_note({})) is None


def test_coverage_fields_on_an_event_that_is_not_a_note_are_not_coverage():
    assert read_coverage(_note(dict(_COVERAGE.data()), event=EventType.CLASSIFY)) is None


def test_a_phase_2_shard_reads_without_a_malformed_line():
    result = store.read_shard(_PHASE_2_SHARD)
    assert result.malformed == 0
    assert len(result.events) == 15
    assert {event.event for event in result.events} == {EventType.COLLECTOR_START, EventType.COLLECTOR_STOP}


def test_a_phase_2_shard_replays_to_the_phase_2_result():
    def utc(day: int, hour: int, minute: int, second: int = 0) -> datetime:
        return datetime(2026, 9, day, hour, minute, second, tzinfo=UTC)

    host = "TEST-HOST"
    timeline = replay(store.read_shard(_PHASE_2_SHARD).events)
    assert timeline.presences == (
        Presence(utc(21, 4, 55), utc(21, 9, 0), host, end_inferred=True),
        Presence(utc(21, 9, 40), utc(21, 13, 30, 30), host, end_inferred=True),
        Presence(utc(21, 13, 45), utc(21, 14, 30), host, end_inferred=False),
        Presence(utc(22, 4, 58), utc(22, 5, 10), host, end_inferred=False),
        Presence(utc(22, 5, 12), utc(22, 13, 0), host, end_inferred=False),
        Presence(utc(22, 13, 5), utc(22, 13, 6), host, end_inferred=False),
        Presence(utc(22, 13, 20), utc(22, 14, 0), host, end_inferred=False),
        Presence(utc(23, 5, 1), None, host, end_inferred=False),
    )
    assert timeline.gaps == (
        Gap(utc(21, 9, 0), utc(21, 9, 40), host, start_inferred=True, end_inferred=False, reason="suspended"),
        Gap(
            utc(21, 13, 30, 30),
            utc(21, 13, 45),
            host,
            start_inferred=True,
            end_inferred=False,
            reason="unclean",
        ),
        Gap(
            utc(21, 14, 30),
            utc(22, 4, 58),
            host,
            start_inferred=False,
            end_inferred=False,
            reason="session_end",
        ),
        Gap(utc(22, 5, 10), utc(22, 5, 12), host, start_inferred=False, end_inferred=False, reason="signal"),
        Gap(
            utc(22, 13, 0),
            utc(22, 13, 5),
            host,
            start_inferred=False,
            end_inferred=False,
            reason="console_close",
        ),
        Gap(
            utc(22, 13, 6),
            utc(22, 13, 20),
            host,
            start_inferred=False,
            end_inferred=False,
            reason="max_ticks",
        ),
        Gap(
            utc(22, 14, 0), utc(23, 5, 1), host, start_inferred=False, end_inferred=False, reason="stop_file"
        ),
    )
