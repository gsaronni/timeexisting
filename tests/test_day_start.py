"""Phase 3 step 8: the day's start resolved from the ledger (roadmap section 4), never from collector liveness."""

import dataclasses
from datetime import UTC, datetime, time

import pytest

from timeexisting import cli, paths
from timeexisting.domain.day_start import DayStart, Provenance, resolve_day_start
from timeexisting.ledger.events import Confidence, Event, EventType, Source
from timeexisting.ledger.replay import IntervalType, replay

# Tuesday 29 September 2026 is the day under test; the 28th is the day before.
_DAY = 29


def _local(day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, minute, second, tzinfo=cli.local_timezone())


def _event(
    ts: datetime,
    event: EventType,
    source: Source = Source.EVENTLOG,
    confidence: Confidence = Confidence.OBSERVED,
    host: str | None = None,
    **data: str,
) -> Event:
    return Event.new(
        ts=ts,
        host=host or paths.current_host(),
        profile="work",
        event=event,
        source=source,
        confidence=confidence,
        data=data,
    )


def _run_start(ts: datetime, session: Source | None = Source.PROBE) -> list[Event]:
    """A collector start, with the startup probe's unlock when the session was unlocked."""
    events = [_event(ts, EventType.COLLECTOR_START, Source.COLLECTOR, reason="launch")]
    if session is not None:
        events.append(_event(ts, EventType.UNLOCK, session, Confidence.INFERRED))
    return events


def _coverage(ts: datetime, span_start: datetime, span_end: datetime) -> Event:
    return _event(
        ts,
        EventType.NOTE,
        Source.COLLECTOR,
        kind="coverage",
        span_start=span_start.astimezone(UTC).isoformat(),
        span_end=span_end.astimezone(UTC).isoformat(),
        system_readable="true",
        system_reaches_back="true",
        winlogon_readable="true",
        winlogon_reaches_back="true",
    )


def _day_start(ts: datetime, start: datetime, detail: str = "prompt") -> Event:
    return _event(
        ts, EventType.DAY_START, Source.USER, start=start.astimezone(UTC).isoformat(), detail=detail
    )


def _resolve(events, cfg, now: datetime | None = None, boot: datetime | None = None) -> DayStart | None:
    return resolve_day_start(replay(events), paths.current_host(), now or _local(_DAY, 10, 0), cfg, boot=boot)


# An evening that ends locked and asleep, the usual state a morning wakes from.
_EVENING = [
    *_run_start(_local(28, 16, 0)),
    _event(_local(28, 16, 30), EventType.LOCK, detail="811 Sens 4"),
    _event(_local(28, 17, 0), EventType.SUSPEND, detail="506 reason 11"),
]


def test_a_collector_that_ran_overnight_still_gives_the_mornings_first_unlock_as_the_start(cfg):
    """The 16 October morning on the dev ledger. The collector started on the 15th at 16:19 local and never stopped: the machine slept from 16:46, the session locked at 16:51 during the standby, the machine woke at 08:46:33 on the 16th and the session unlocked at 08:46:41. No collector run starts on the 16th; the typed timeline opens the day's first presence at that unlock, observed, and so does the start."""
    october = datetime(2026, 10, 16, 10, 0, tzinfo=cli.local_timezone())
    events = [
        *_run_start(datetime(2026, 10, 15, 14, 19, 7, tzinfo=UTC)),
        _event(datetime(2026, 10, 15, 14, 46, 26, tzinfo=UTC), EventType.SUSPEND, detail="506 reason 15"),
        _event(datetime(2026, 10, 15, 14, 51, 1, tzinfo=UTC), EventType.LOCK, detail="811 Sens 4"),
        _event(datetime(2026, 10, 16, 6, 46, 33, tzinfo=UTC), EventType.RESUME, detail="507 reason 15"),
        _event(datetime(2026, 10, 16, 6, 46, 41, tzinfo=UTC), EventType.UNLOCK, detail="811 Sens 5"),
    ]
    timeline = replay(events)
    present = [interval for interval in timeline.intervals if interval.kind is IntervalType.PRESENT]
    assert present[-1].start == datetime(2026, 10, 16, 6, 46, 41, tzinfo=UTC)
    assert not [
        run for run in timeline.presences if run.start.astimezone(october.tzinfo).date() == october.date()
    ]

    assert _resolve(events, cfg, october) == DayStart(time(8, 46), Provenance.OBSERVED)


def test_a_dark_wake_is_not_the_start_but_the_unlock_after_it_is(cfg):
    """The 26 September morning: a dark wake at 07:50 onto a locked session, then the real unlock at 09:02."""
    dark_wake = [
        *_EVENING,
        _event(_local(_DAY, 7, 50), EventType.RESUME, detail="507 reason 20"),
        _event(_local(_DAY, 7, 52), EventType.SUSPEND, detail="506 reason 12"),
        _event(_local(_DAY, 9, 2), EventType.RESUME, detail="507 reason 32"),
        _event(_local(_DAY, 9, 2, 10), EventType.UNLOCK, detail="811 Sens 5"),
    ]
    assert _resolve(dark_wake, cfg) == DayStart(time(9, 2), Provenance.OBSERVED)

    real_wake = [*dark_wake, _event(_local(_DAY, 7, 50, 5), EventType.UNLOCK, detail="811 Sens 5")]
    assert _resolve(real_wake, cfg) == DayStart(time(7, 50), Provenance.OBSERVED)


def test_presence_open_at_the_earliest_credit_starts_the_day_at_the_clamp(cfg):
    assert cfg.credit.earliest == time(7, 0)
    early = [
        *_EVENING,
        _event(_local(_DAY, 6, 50), EventType.RESUME),
        _event(_local(_DAY, 6, 50), EventType.UNLOCK),
    ]
    assert _resolve(early, cfg) == DayStart(time(7, 0), Provenance.OBSERVED)

    later = [
        *_EVENING,
        _event(_local(_DAY, 7, 10), EventType.RESUME),
        _event(_local(_DAY, 7, 10), EventType.UNLOCK),
    ]
    assert _resolve(later, cfg) == DayStart(time(7, 10), Provenance.OBSERVED)


def test_presence_that_closes_before_the_earliest_credit_is_never_a_start(cfg):
    night = [
        *_EVENING,
        _event(_local(_DAY, 2, 0), EventType.RESUME),
        _event(_local(_DAY, 2, 0), EventType.UNLOCK),
        _event(_local(_DAY, 2, 10), EventType.LOCK),
        _event(_local(_DAY, 8, 55), EventType.UNLOCK),
    ]
    assert _resolve(night, cfg) == DayStart(time(8, 55), Provenance.OBSERVED)

    no_lock_at_night = [
        event for event in night if not (event.event is EventType.LOCK and event.ts > _local(_DAY, 0, 0))
    ]
    assert _resolve(no_lock_at_night, cfg) == DayStart(time(7, 0), Provenance.OBSERVED)

    nothing_after = night[:-1]
    assert _resolve(nothing_after, cfg) is None


def test_a_resume_onto_an_unlocked_session_is_not_a_start_until_input_arrives(cfg):
    unlocked_sleep = [*_run_start(_local(28, 16, 0)), _event(_local(28, 17, 0), EventType.SUSPEND)]
    unevidenced = [*unlocked_sleep, _event(_local(_DAY, 7, 20), EventType.RESUME, detail="507 reason 55")]
    assert replay(unevidenced).intervals[-1].kind is IntervalType.UNEVIDENCED
    assert _resolve(unevidenced, cfg) is None

    evidenced = [*unevidenced, _event(_local(_DAY, 7, 30), EventType.INPUT, Source.WIN32)]
    # The session the input lands on was opened by the startup probe, so the presence it evidences is inferred (step 9b); from a logon it is observed.
    assert _resolve(evidenced, cfg) == DayStart(time(7, 30), Provenance.INFERRED)
    from_logon = [
        _event(_local(28, 16, 0), EventType.COLLECTOR_START, Source.COLLECTOR, reason="launch"),
        _event(_local(28, 16, 0), EventType.UNLOCK, Source.LOGON),
        *unlocked_sleep[2:],
        *unevidenced[3:],
        evidenced[-1],
    ]
    assert _resolve(from_logon, cfg) == DayStart(time(7, 30), Provenance.OBSERVED)


def test_collector_liveness_alone_is_never_a_start(cfg):
    """Legacy lines: collector starts and stops with no session event replay to `unevidenced`."""
    legacy = [
        _event(_local(_DAY, 8, 5), EventType.COLLECTOR_START, Source.COLLECTOR, reason="launch"),
        _event(_local(_DAY, 8, 40), EventType.COLLECTOR_STOP, Source.COLLECTOR, reason="suspended"),
        _event(_local(_DAY, 9, 15), EventType.COLLECTOR_START, Source.COLLECTOR, reason="resumed"),
    ]
    assert [interval.kind for interval in replay(legacy).intervals] == [
        IntervalType.UNEVIDENCED,
        IntervalType.ASLEEP,
        IntervalType.UNEVIDENCED,
    ]
    assert _resolve(legacy, cfg) is None

    with_an_unlock = [*legacy, _event(_local(_DAY, 8, 6), EventType.UNLOCK)]
    assert _resolve(with_an_unlock, cfg) == DayStart(time(8, 6), Provenance.OBSERVED)


def test_provenance_follows_the_opening_boundary(cfg):
    probe = _run_start(_local(_DAY, 8, 5), Source.PROBE)
    assert _resolve(probe, cfg) == DayStart(time(8, 5), Provenance.INFERRED)

    observed = [*_run_start(_local(_DAY, 8, 5), None), _event(_local(_DAY, 8, 5), EventType.UNLOCK)]
    assert _resolve(observed, cfg) == DayStart(time(8, 5), Provenance.OBSERVED)

    # A gap both logs cover is reconstructed, so the logon unlock inside it opens presence itself.
    logon = [
        *_EVENING,
        _event(_local(28, 17, 30), EventType.COLLECTOR_STOP, Source.COLLECTOR, reason="session_end"),
        _event(_local(_DAY, 7, 57), EventType.RESUME, detail="507 reason 1"),
        _event(_local(_DAY, 7, 58), EventType.UNLOCK, Source.LOGON),
        *_run_start(_local(_DAY, 8, 5), None),
        _coverage(_local(_DAY, 8, 5), _local(28, 17, 30), _local(_DAY, 8, 5)),
    ]
    assert _resolve(logon, cfg) == DayStart(time(7, 58), Provenance.LOGON)


def test_presence_open_since_the_day_before_starts_the_day_at_the_clamp(cfg):
    still_open = _run_start(_local(28, 16, 0))
    assert replay(still_open).intervals[-1].end is None
    assert _resolve(still_open, cfg) == DayStart(time(7, 0), Provenance.INFERRED)


def test_the_boot_time_is_a_sanity_clamp_never_a_start(cfg):
    """Rule 4: the collector is down and the ledger holds no boot, but the machine booted today at 09:00, so the presence open since yesterday is a session the ledger lost track of."""
    still_open = _run_start(_local(28, 16, 0))
    assert _resolve(still_open, cfg, boot=_local(_DAY, 9, 0)) is None
    assert _resolve(still_open, cfg, boot=_local(28, 12, 0)) == DayStart(time(7, 0), Provenance.INFERRED)
    assert _resolve(still_open, cfg, boot=None) == DayStart(time(7, 0), Provenance.INFERRED)

    # The boot clamps the interval it postdates, not the day: presence opened after it still counts.
    relocked = [
        *still_open,
        _event(_local(_DAY, 8, 0), EventType.LOCK),
        _event(_local(_DAY, 9, 30), EventType.UNLOCK),
    ]
    assert _resolve(relocked, cfg, boot=_local(_DAY, 9, 0)) == DayStart(time(9, 30), Provenance.OBSERVED)
    assert _resolve(relocked, cfg, boot=_local(28, 12, 0)) == DayStart(time(7, 0), Provenance.INFERRED)


def test_other_hosts_the_day_before_and_the_future_are_ignored(cfg):
    elsewhere = [
        _event(
            _local(_DAY, 8, 0), EventType.COLLECTOR_START, Source.COLLECTOR, host="elsewhere", reason="launch"
        ),
        _event(_local(_DAY, 8, 0), EventType.UNLOCK, host="elsewhere"),
    ]
    assert _resolve(elsewhere, cfg) is None

    yesterday = [
        *_run_start(_local(28, 8, 0), None),
        _event(_local(28, 8, 0), EventType.UNLOCK),
        _event(_local(28, 17, 0), EventType.LOCK),
    ]
    assert _resolve(yesterday, cfg) is None

    later_today = [*_run_start(_local(_DAY, 11, 0), None), _event(_local(_DAY, 11, 0), EventType.UNLOCK)]
    assert _resolve(later_today, cfg, now=_local(_DAY, 10, 0)) is None
    assert _resolve(later_today, cfg, now=_local(_DAY, 12, 0)) == DayStart(time(11, 0), Provenance.OBSERVED)


_OBSERVED_MORNING = [*_run_start(_local(_DAY, 9, 2), None), _event(_local(_DAY, 9, 2), EventType.UNLOCK)]


def test_a_recorded_day_start_wins_over_the_evidence(cfg):
    recorded = [*_OBSERVED_MORNING, _day_start(_local(_DAY, 10, 5), _local(_DAY, 8, 55))]
    assert _resolve(recorded, cfg) == DayStart(time(8, 55), Provenance.PROMPT)

    flagged = [*_OBSERVED_MORNING, _day_start(_local(_DAY, 10, 5), _local(_DAY, 8, 55), detail="flag")]
    assert _resolve(flagged, cfg) == DayStart(time(8, 55), Provenance.FLAG)

    assert _resolve(_OBSERVED_MORNING, cfg) == DayStart(time(9, 2), Provenance.OBSERVED)


def test_a_later_day_start_supersedes_an_earlier_one(cfg):
    first = _day_start(_local(_DAY, 10, 5), _local(_DAY, 8, 55))
    second = _day_start(_local(_DAY, 10, 13), _local(_DAY, 8, 40), detail="flag")
    assert _resolve([*_OBSERVED_MORNING, second, first], cfg) == DayStart(time(8, 40), Provenance.FLAG)
    assert _resolve([*_OBSERVED_MORNING, first], cfg) == DayStart(time(8, 55), Provenance.PROMPT)


@pytest.mark.parametrize("ids", [("1", "2"), ("2", "1")])
def test_day_starts_at_one_time_follow_id_order(cfg, ids):
    first = dataclasses.replace(
        _day_start(_local(_DAY, 10, 5), _local(_DAY, 8, 55)), id=ids[0].rjust(32, "0")
    )
    second = dataclasses.replace(
        _day_start(_local(_DAY, 10, 5), _local(_DAY, 8, 40)), id=ids[1].rjust(32, "0")
    )
    expected = time(8, 40) if ids[0] < ids[1] else time(8, 55)
    assert _resolve([second, first], cfg) == DayStart(expected, Provenance.PROMPT)


def test_a_day_start_for_another_day_or_host_is_ignored(cfg):
    yesterdays = [*_OBSERVED_MORNING, _day_start(_local(28, 10, 5), _local(28, 8, 55))]
    assert _resolve(yesterdays, cfg) == DayStart(time(9, 2), Provenance.OBSERVED)

    # Recorded today, but for yesterday's date: the start's own day counts, not the record's.
    for_yesterday = [*_OBSERVED_MORNING, _day_start(_local(_DAY, 10, 5), _local(28, 8, 55))]
    assert _resolve(for_yesterday, cfg) == DayStart(time(9, 2), Provenance.OBSERVED)

    elsewhere = [
        *_OBSERVED_MORNING,
        _event(
            _local(_DAY, 10, 5),
            EventType.DAY_START,
            Source.USER,
            host="elsewhere",
            start=_local(_DAY, 8, 55).astimezone(UTC).isoformat(),
            detail="prompt",
        ),
    ]
    assert _resolve(elsewhere, cfg) == DayStart(time(9, 2), Provenance.OBSERVED)


@pytest.mark.parametrize("start", ["", "not a time", "2026-09-29T08:55:00", "08:55"])
def test_a_day_start_whose_start_is_unreadable_is_ignored(cfg, start):
    data = {"detail": "prompt"} | ({"start": start} if start else {})
    broken = [*_OBSERVED_MORNING, _event(_local(_DAY, 10, 5), EventType.DAY_START, Source.USER, **data)]
    assert _resolve(broken, cfg) == DayStart(time(9, 2), Provenance.OBSERVED)
