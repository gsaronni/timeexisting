import logging
import os
from datetime import UTC, datetime, timedelta

from timeexisting import paths
from timeexisting.ledger import store
from timeexisting.ledger.events import Event, EventType, Source, to_json

_T0 = datetime(2026, 9, 17, 6, 0, 0, tzinfo=UTC)


def _event(host: str = "laptop", minutes: int = 0, **overrides) -> Event:
    fields = {
        "ts": _T0 + timedelta(minutes=minutes),
        "host": host,
        "profile": "work",
        "event": EventType.COLLECTOR_START,
        "source": Source.COLLECTOR,
    }
    return Event.new(**(fields | overrides))


def test_shard_path_is_host_jsonl_under_ledger_dir():
    assert store.shard_path("TEST-HOST") == paths.ledger_dir() / "TEST-HOST.jsonl"


def test_append_then_read():
    first, second = _event(minutes=0), _event(minutes=1, event=EventType.COLLECTOR_STOP)
    store.append(first)
    store.append(second)
    assert store.read_shard(store.shard_path("laptop")) == store.ReadResult(
        events=(first, second), malformed=0
    )


def test_each_append_is_on_disk_immediately():
    path = store.shard_path("laptop")
    for count in range(1, 4):
        store.append(_event(minutes=count))
        assert len(path.read_bytes().splitlines()) == count


def test_append_writes_exactly_one_utf8_lf_terminated_line():
    event = _event()
    store.append(event)
    assert store.shard_path("laptop").read_bytes() == to_json(event).encode("utf-8") + b"\n"


def test_non_ascii_data_round_trips_as_utf8():
    event = _event(event=EventType.NOTE, source=Source.MANUAL, data={"place": "Hvidovre, Ø"})
    store.append(event)

    raw = store.shard_path("laptop").read_bytes()
    assert "Hvidovre, Ø".encode() in raw
    assert b"\\u00d8" not in raw
    (restored,) = store.read_shard(store.shard_path("laptop")).events
    assert restored == event
    assert restored.data["place"] == "Hvidovre, Ø"


def test_fsync_called_once_per_append(monkeypatch):
    calls: list[int] = []
    real_fsync = os.fsync

    def counting_fsync(descriptor: int) -> None:
        calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", counting_fsync)
    for minute in range(3):
        store.append(_event(minutes=minute))
    assert len(calls) == 3


def test_append_does_not_disturb_existing_lines():
    path = store.shard_path("laptop")
    path.write_bytes(b"hand-written line kept as is\n")
    store.append(_event())
    assert path.read_bytes().startswith(b"hand-written line kept as is\n")


def test_append_after_a_torn_line_keeps_the_new_event_intact():
    path = store.shard_path("laptop")
    earlier = _event(minutes=0)
    torn = to_json(_event(minutes=1)).encode("utf-8")[:40]
    path.write_bytes(to_json(earlier).encode("utf-8") + b"\n" + torn)

    event = _event(minutes=2)
    store.append(event)

    assert (
        path.read_bytes()
        == to_json(earlier).encode("utf-8") + b"\n" + torn + b"\n" + to_json(event).encode("utf-8") + b"\n"
    )
    result = store.read_shard(path)
    assert result.events == (earlier, event)
    assert result.malformed == 1


def test_append_to_a_shard_holding_only_a_torn_line():
    path = store.shard_path("laptop")
    path.write_bytes(b'{"v": 1, "id": "tor')
    event = _event()
    store.append(event)
    assert store.read_shard(path) == store.ReadResult(events=(event,), malformed=1)


def test_no_extra_newline_when_the_shard_ends_cleanly():
    first, second = _event(minutes=0), _event(minutes=1)
    store.append(first)
    store.append(second)
    assert b"\n\n" not in store.shard_path("laptop").read_bytes()


def test_missing_shard_reads_empty():
    assert store.read_shard(paths.ledger_dir() / "nobody.jsonl") == store.ReadResult(events=(), malformed=0)


def test_malformed_lines_skipped_counted_and_logged(caplog):
    good_one, good_two = _event(minutes=0), _event(minutes=1)
    path = store.shard_path("laptop")
    path.write_bytes(
        b"\n".join(
            [
                to_json(good_one).encode("utf-8"),
                b"not json at all",
                b"",
                b'{"v": 2}',
                b"\xff\xfe invalid utf-8",
                to_json(good_two).encode("utf-8"),
                b'{"v": 1, "id": "torn',
            ]
        )
    )
    with caplog.at_level(logging.WARNING, logger="timeexisting.ledger.store"):
        result = store.read_shard(path)

    assert result.events == (good_one, good_two)
    assert result.malformed == 4
    warned_lines = [record.getMessage() for record in caplog.records]
    assert len(warned_lines) == 4
    for number in (2, 4, 5, 7):
        assert any(f"laptop.jsonl:{number}:" in message for message in warned_lines)


def test_crlf_from_a_hand_edit_is_tolerated():
    event = _event()
    store.shard_path("laptop").write_bytes(to_json(event).encode("utf-8") + b"\r\n")
    assert store.read_shard(store.shard_path("laptop")).events == (event,)


def test_read_all_merges_shards_in_timestamp_order():
    laptop = [_event("laptop", minutes=minute) for minute in (0, 2, 4)]
    desktop = [_event("desktop", minutes=minute) for minute in (1, 3, 5)]
    for event in laptop + desktop:
        store.append(event)
    store.shard_path("desktop").write_bytes(store.shard_path("desktop").read_bytes() + b"garbage\n")

    result = store.read_all()
    assert [event.ts for event in result.events] == [_T0 + timedelta(minutes=m) for m in range(6)]
    assert [event.host for event in result.events] == ["laptop", "desktop"] * 3
    assert result.malformed == 1


def test_read_all_breaks_timestamp_ties_by_id():
    same_moment = [_event(host) for host in ("b-host", "a-host", "c-host")]
    for event in same_moment:
        store.append(event)
    assert store.read_all().events == tuple(sorted(same_moment, key=lambda event: event.id))


def test_read_all_ignores_other_files():
    store.append(_event())
    (paths.ledger_dir() / "notes.txt").write_text("not a shard", encoding="utf-8")
    assert len(store.read_all().events) == 1


def test_read_all_on_an_empty_ledger():
    assert store.read_all() == store.ReadResult(events=(), malformed=0)
