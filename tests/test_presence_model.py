"""The typed timeline of roadmap section 6, from synthetic events only. Times are written in local Copenhagen time for readability and stored in UTC like every event."""

import dataclasses
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise

import pytest

from tests.timeline_helpers import (
    _CPH,
    ASLEEP,
    AWAY,
    DOWNTIME,
    INF,
    OFF,
    PRESENT,
    UNEVIDENCED,
    _event,
    _t,
    boot,
    covered,
    intervals,
    iv,
    lock,
    present_time,
    resume,
    start,
    stop,
    suspend,
    unlock,
    user_input,
)
from timeexisting.ledger.events import Event, EventType, Source, order_key
from timeexisting.ledger.replay import _boundaries, _Step, day_view, replay

# The named cases of phase 3 step 3.


def test_plain_working_day():
    # Logon, lock at lunch, unlock, lock in the evening, sleep.
    assert intervals(
        unlock(_t(13, 7, 59, 40), Source.LOGON),
        start(_t(13, 8, 0, 5)),
        lock(_t(13, 11, 0)),
        unlock(_t(13, 11, 30)),
        lock(_t(13, 16, 30)),
        suspend(_t(13, 16, 35)),
    ) == (
        iv(PRESENT, _t(13, 8, 0, 5), _t(13, 11, 0), Source.COLLECTOR),
        iv(AWAY, _t(13, 11, 0), _t(13, 11, 30)),
        iv(PRESENT, _t(13, 11, 30), _t(13, 16, 30)),
        iv(AWAY, _t(13, 16, 30), _t(13, 16, 35)),
        iv(ASLEEP, _t(13, 16, 35), None),
    )


def test_dark_wake_with_the_session_locked_is_away_and_never_present():
    timeline = intervals(
        start(_t(13, 16, 0)),
        unlock(_t(13, 16, 0, 10)),
        lock(_t(13, 17, 0)),
        suspend(_t(13, 17, 5)),
        resume(_t(14, 3, 10)),
        suspend(_t(14, 3, 11)),
    )
    assert timeline == (
        iv(UNEVIDENCED, _t(13, 16, 0), _t(13, 16, 0, 10), Source.COLLECTOR),
        iv(PRESENT, _t(13, 16, 0, 10), _t(13, 17, 0)),
        iv(AWAY, _t(13, 17, 0), _t(13, 17, 5)),
        iv(ASLEEP, _t(13, 17, 5), _t(14, 3, 10)),
        iv(AWAY, _t(14, 3, 10), _t(14, 3, 11)),
        iv(ASLEEP, _t(14, 3, 11), None),
    )
    assert present_time(timeline, _t(13, 17, 0), _t(14, 12, 0)) == timedelta()


def test_dark_wake_on_an_unlocked_session_is_unevidenced_until_input():
    assert intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        suspend(_t(13, 12, 0)),
        resume(_t(13, 12, 3)),
        user_input(_t(13, 12, 4)),
    ) == (
        iv(PRESENT, _t(13, 8, 0), _t(13, 12, 0)),
        iv(ASLEEP, _t(13, 12, 0), _t(13, 12, 3)),
        iv(UNEVIDENCED, _t(13, 12, 3), _t(13, 12, 4)),
        iv(PRESENT, _t(13, 12, 4), None, Source.WIN32),
    )


def test_input_from_a_wake_reason_opens_presence_at_its_own_time():
    assert intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        suspend(_t(13, 12, 0)),
        resume(_t(13, 12, 3)),
        user_input(_t(13, 12, 3), Source.EVENTLOG),
    )[-1] == iv(PRESENT, _t(13, 12, 3), None)


def test_input_on_a_locked_or_sleeping_session_opens_nothing():
    timeline = intervals(
        start(_t(13, 8, 0)),
        lock(_t(13, 8, 0)),
        user_input(_t(13, 8, 30)),
        unlock(_t(13, 9, 0)),
        suspend(_t(13, 10, 0)),
        user_input(_t(13, 10, 30)),
        resume(_t(13, 11, 0)),
    )
    assert timeline == (
        iv(AWAY, _t(13, 8, 0), _t(13, 9, 0)),
        iv(PRESENT, _t(13, 9, 0), _t(13, 10, 0)),
        iv(ASLEEP, _t(13, 10, 0), _t(13, 11, 0)),
        iv(UNEVIDENCED, _t(13, 11, 0), None),
    )


def test_lock_then_sleep_then_wake_then_unlock():
    assert [
        interval.kind
        for interval in intervals(
            start(_t(13, 8, 0)),
            unlock(_t(13, 8, 0)),
            lock(_t(13, 12, 0)),
            suspend(_t(13, 12, 10)),
            resume(_t(13, 13, 0)),
            unlock(_t(13, 13, 2)),
        )
    ] == [PRESENT, AWAY, ASLEEP, AWAY, PRESENT]


def test_unclean_stop_is_downtime_from_the_checkpoint_to_the_next_start():
    assert intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 1)),
        stop(_t(13, 10, 0), "unclean", INF),
        start(_t(13, 10, 20)),
    ) == (
        iv(UNEVIDENCED, _t(13, 8, 0), _t(13, 8, 1), Source.COLLECTOR),
        iv(PRESENT, _t(13, 8, 1), _t(13, 10, 0)),
        iv(DOWNTIME, _t(13, 10, 0), _t(13, 10, 20), Source.COLLECTOR, INF),
        iv(UNEVIDENCED, _t(13, 10, 20), None, Source.COLLECTOR),
    )


def test_after_an_unclean_stop_a_startup_probe_reopens_presence_as_inferred():
    restart = _t(13, 10, 20)
    timeline = intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        stop(_t(13, 10, 0), "unclean", INF),
        start(restart),
        unlock(restart, Source.PROBE, INF),
    )
    assert timeline[-1] == iv(PRESENT, restart, None, Source.PROBE, INF)


def test_a_power_suspend_after_a_stop_does_not_pair_with_an_eventlog_suspend_before_it():
    timeline = intervals(
        start(_t(13, 8, 0)),
        lock(_t(13, 8, 0)),
        suspend(_t(13, 9, 0)),
        stop(_t(13, 9, 5), "unclean", INF),
        start(_t(13, 10, 0)),
        lock(_t(13, 10, 0), Source.PROBE, INF),
        suspend(_t(13, 11, 0), Source.POWER),
    )
    assert timeline[-2:] == (
        iv(AWAY, _t(13, 10, 0), _t(13, 11, 0), Source.PROBE, INF),
        iv(ASLEEP, _t(13, 11, 0), None, Source.POWER),
    )


def test_every_stop_closes_presence_until_the_next_start():
    for reason in ("unclean", "session_end", "stop_file", "signal", "console_close", "max_ticks"):
        timeline = intervals(
            start(_t(13, 8, 0)),
            unlock(_t(13, 8, 0)),
            stop(_t(13, 9, 0), reason),
            start(_t(13, 9, 30)),
        )
        assert [interval.kind for interval in timeline] == [PRESENT, DOWNTIME, UNEVIDENCED], reason


def test_an_unlock_inside_a_gap_opens_presence_only_from_the_next_start():
    timeline = intervals(
        start(_t(13, 8, 0)),
        stop(_t(13, 9, 0), "stop_file"),
        unlock(_t(13, 9, 10)),
        start(_t(13, 9, 30)),
    )
    assert timeline[-2:] == (
        iv(DOWNTIME, _t(13, 9, 0), _t(13, 9, 30), Source.COLLECTOR),
        iv(PRESENT, _t(13, 9, 30), None, Source.COLLECTOR),
    )


def test_midnight_crossing_is_one_interval_in_storage_and_split_only_in_the_day_view():
    timeline = intervals(
        start(_t(16, 23, 0)),
        unlock(_t(16, 23, 30)),
        lock(_t(17, 0, 45)),
    )
    crossing = iv(PRESENT, _t(16, 23, 30), _t(17, 0, 45))
    assert crossing in timeline
    now = _t(17, 10, 0)
    assert iv(PRESENT, _t(16, 23, 30), _t(17, 0, 0)) in day_view(timeline, date(2026, 10, 16), _CPH, now)
    assert day_view(timeline, date(2026, 10, 17), _CPH, now) == (
        iv(PRESENT, _t(17, 0, 0), _t(17, 0, 45)),
        iv(AWAY, _t(17, 0, 45), now),
    )


def test_the_day_view_ends_an_interval_open_since_yesterday_at_todays_midnight():
    timeline = intervals(
        start(_t(16, 8, 0)),
        unlock(_t(16, 8, 0)),
        lock(_t(16, 17, 0)),
        suspend(_t(16, 17, 5)),
    )
    assert timeline[-1] == iv(ASLEEP, _t(16, 17, 5), None)
    yesterday = day_view(timeline, date(2026, 10, 16), _CPH, now=_t(17, 10, 0))
    assert yesterday[-1] == iv(ASLEEP, _t(16, 17, 5), _t(17, 0, 0))


def test_the_day_view_drops_an_interval_starting_at_or_after_now():
    timeline = intervals(start(_t(17, 8, 0)), unlock(_t(17, 9, 0)))
    assert day_view(timeline, date(2026, 10, 17), _CPH, now=_t(17, 9, 0)) == (
        iv(UNEVIDENCED, _t(17, 8, 0), _t(17, 9, 0), Source.COLLECTOR),
    )
    assert day_view(timeline, date(2026, 10, 17), _CPH, now=_t(17, 10, 0)) == (
        iv(UNEVIDENCED, _t(17, 8, 0), _t(17, 9, 0), Source.COLLECTOR),
        iv(PRESENT, _t(17, 9, 0), _t(17, 10, 0)),
    )


def test_dst_end_moves_no_stored_timestamp():
    # 25 October 2026: 03:00 CEST becomes 02:00 CET. 01:50 CEST to 03:10 CET spans the repeated hour.
    begin = datetime(2026, 10, 25, 1, 50, tzinfo=_CPH)
    end = datetime(2026, 10, 25, 3, 10, tzinfo=_CPH)
    timeline = intervals(start(begin), unlock(begin), lock(end))
    present, _ = timeline
    assert present == iv(PRESENT, begin.astimezone(UTC), end.astimezone(UTC))
    assert present.start == datetime(2026, 10, 24, 23, 50, tzinfo=UTC)
    assert present.end == datetime(2026, 10, 25, 2, 10, tzinfo=UTC)
    assert present.end - present.start == timedelta(hours=2, minutes=20)
    now = datetime(2026, 10, 25, 12, 0, tzinfo=_CPH).astimezone(UTC)
    assert day_view(timeline, date(2026, 10, 25), _CPH, now) == (present, iv(AWAY, end.astimezone(UTC), now))


def test_the_dst_end_day_view_is_25_hours_long():
    timeline = intervals(start(_t(24, 12, 0)), unlock(_t(24, 12, 0)), lock(_t(26, 12, 0)))
    (day,) = day_view(timeline, date(2026, 10, 25), _CPH, now=_t(27, 12, 0))
    assert day.end - day.start == timedelta(hours=25)


def test_26_september_dark_wake_gives_zero_present_time():
    # Locked in the evening; at 07:50 a 507, a hibernate-from-sleep 42, a resume 15 s later; the real morning unlock at 08:40.
    def sep(day: int, hour: int, minute: int, second: int = 0) -> datetime:
        return _t(day, hour, minute, second, month=9)

    timeline = intervals(
        start(sep(25, 8, 0)),
        unlock(sep(25, 8, 0)),
        lock(sep(25, 18, 0)),
        suspend(sep(25, 18, 5)),
        suspend(sep(25, 18, 5, 1), Source.POWER),
        resume(sep(26, 7, 50, 0)),
        resume(sep(26, 7, 50, 2), Source.POWER),
        suspend(sep(26, 7, 50, 20)),
        suspend(sep(26, 7, 50, 21), Source.POWER),
        resume(sep(26, 7, 50, 35)),
        resume(sep(26, 7, 50, 36), Source.POWER),
        suspend(sep(26, 7, 55, 0)),
        resume(sep(26, 8, 39, 50)),
        unlock(sep(26, 8, 40, 0)),
        unlock(sep(26, 8, 40, 1), Source.WTS),
    )
    assert present_time(timeline, sep(25, 18, 0), sep(26, 8, 40)) == timedelta()
    assert [interval for interval in timeline if interval.kind is PRESENT][-1].start == sep(26, 8, 40)


def test_26_september_on_a_phase_2_shard_gives_zero_present_time():
    def sep(day: int, hour: int, minute: int, second: int = 0) -> datetime:
        return _t(day, hour, minute, second, month=9)

    timeline = intervals(
        start(sep(25, 8, 0)),
        stop(sep(25, 18, 5), "suspended", INF),
        start(sep(26, 7, 50), "resumed"),
        stop(sep(26, 7, 50), "suspended", INF),
        start(sep(26, 8, 40), "resumed"),
    )
    assert present_time(timeline, sep(25, 8, 0), sep(26, 12, 0)) == timedelta()
    assert timeline == (
        iv(UNEVIDENCED, sep(25, 8, 0), sep(25, 18, 5), Source.COLLECTOR),
        iv(ASLEEP, sep(25, 18, 5), sep(26, 8, 40), Source.COLLECTOR, INF),
        iv(UNEVIDENCED, sep(26, 8, 40), None, Source.COLLECTOR),
    )


# Timestamp authority.


def test_a_wts_lock_arriving_hours_after_its_eventlog_record_is_one_boundary_at_the_record():
    timeline = intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        lock(_t(13, 10, 0)),
        lock(_t(13, 13, 0), Source.WTS),
        unlock(_t(13, 14, 0)),
    )
    assert timeline == (
        iv(PRESENT, _t(13, 8, 0), _t(13, 10, 0)),
        iv(AWAY, _t(13, 10, 0), _t(13, 14, 0)),
        iv(PRESENT, _t(13, 14, 0), None),
    )


def test_a_lock_during_standby_received_at_the_wake_lands_inside_the_sleep():
    timeline = intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        suspend(_t(13, 12, 0)),
        lock(_t(13, 12, 5)),
        resume(_t(13, 15, 0)),
        lock(_t(13, 15, 0, 2), Source.WTS),
    )
    assert timeline[-2:] == (iv(ASLEEP, _t(13, 12, 0), _t(13, 15, 0)), iv(AWAY, _t(13, 15, 0), None))


def test_a_duplicated_eventlog_record_counts_once():
    # Two live locks before one eventlog lock: the first pairs with it, the second keeps its own time. Counted twice, the duplicate would absorb the second and move the boundary to the record.
    events = [
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        lock(_t(13, 9, 30), Source.WTS),
        lock(_t(13, 9, 31), Source.PROBE, INF),
        lock(_t(13, 10, 0)),
    ]
    single = intervals(*events)
    duplicated = intervals(*events, lock(_t(13, 10, 0)))
    assert duplicated == single
    assert single[-1] == iv(AWAY, _t(13, 9, 31), None, Source.PROBE, INF)


def test_eventlog_records_with_different_codes_are_not_duplicates():
    events = [
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        lock(_t(13, 9, 30), Source.WTS),
        lock(_t(13, 9, 31), Source.PROBE, INF),
        lock(_t(13, 10, 0)),
        lock(_t(13, 10, 0), detail="Winlogon/Operational 811 Sens 3"),
    ]
    assert intervals(*events)[-1] == iv(AWAY, _t(13, 10, 0), None)


def test_a_wts_transition_with_no_eventlog_partner_keeps_its_time():
    assert intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0), Source.WTS),
        lock(_t(13, 10, 0), Source.WTS),
        unlock(_t(13, 10, 30), Source.WTS),
    ) == (
        iv(PRESENT, _t(13, 8, 0), _t(13, 10, 0), Source.WTS),
        iv(AWAY, _t(13, 10, 0), _t(13, 10, 30), Source.WTS),
        iv(PRESENT, _t(13, 10, 30), None, Source.WTS),
    )


def test_a_probe_unlock_at_collector_start_takes_the_earlier_eventlog_unlock():
    timeline = intervals(
        start(_t(13, 8, 0)),
        lock(_t(13, 8, 30)),
        stop(_t(13, 9, 0), "unclean", INF),
        unlock(_t(13, 9, 5)),
        start(_t(13, 9, 10)),
        unlock(_t(13, 9, 10), Source.PROBE, INF),
    )
    # One boundary, at the record inside the gap: presence opens with the start, observed. Without the record it opens with the probe, inferred.
    assert timeline[-1] == iv(PRESENT, _t(13, 9, 10), None, Source.COLLECTOR)
    probe_only = intervals(
        start(_t(13, 8, 0)),
        lock(_t(13, 8, 30)),
        stop(_t(13, 9, 0), "unclean", INF),
        start(_t(13, 9, 10)),
        unlock(_t(13, 9, 10), Source.PROBE, INF),
    )
    assert probe_only[-1] == iv(PRESENT, _t(13, 9, 10), None, Source.PROBE, INF)


def test_a_probe_unlock_while_running_takes_the_earlier_eventlog_unlock():
    timeline = intervals(
        start(_t(13, 8, 0)),
        lock(_t(13, 9, 0)),
        unlock(_t(13, 9, 58)),
        unlock(_t(13, 10, 0), Source.PROBE, INF),
    )
    assert timeline[-2:] == (iv(AWAY, _t(13, 9, 0), _t(13, 9, 58)), iv(PRESENT, _t(13, 9, 58), None))


def test_a_live_transition_earlier_than_its_record_moves_to_the_record():
    timeline = intervals(
        start(_t(13, 8, 0)),
        lock(_t(13, 9, 0), Source.WTS),
        lock(_t(13, 9, 0, 1)),
        unlock(_t(13, 9, 57, 59), Source.WTS),
        unlock(_t(13, 9, 58)),
    )
    assert timeline == (
        iv(UNEVIDENCED, _t(13, 8, 0), _t(13, 9, 0, 1), Source.COLLECTOR),
        iv(AWAY, _t(13, 9, 0, 1), _t(13, 9, 58)),
        iv(PRESENT, _t(13, 9, 58), None),
    )


def test_a_power_resume_takes_the_earlier_507_time():
    timeline = intervals(
        start(_t(13, 8, 0)),
        lock(_t(13, 8, 0)),
        suspend(_t(13, 12, 0)),
        suspend(_t(13, 12, 0, 1), Source.POWER),
        resume(_t(13, 13, 0)),
        resume(_t(13, 13, 0, 4), Source.POWER),
    )
    assert timeline[-2:] == (iv(ASLEEP, _t(13, 12, 0), _t(13, 13, 0)), iv(AWAY, _t(13, 13, 0), None))


def test_a_power_resume_with_no_power_suspend_still_pairs_and_the_eventlog_suspend_keeps_its_time():
    timeline = intervals(
        start(_t(13, 8, 0)),
        lock(_t(13, 8, 0)),
        suspend(_t(13, 12, 0)),
        resume(_t(13, 13, 0)),
        resume(_t(13, 13, 0, 4), Source.POWER),
    )
    assert timeline[-2:] == (iv(ASLEEP, _t(13, 12, 0), _t(13, 13, 0)), iv(AWAY, _t(13, 13, 0), None))


def test_a_clock_suspend_with_no_eventlog_partner_keeps_its_inferred_time():
    timeline = intervals(
        start(_t(13, 8, 0)),
        lock(_t(13, 8, 0)),
        suspend(_t(13, 12, 0), Source.CLOCK, INF),
        resume(_t(13, 13, 0), Source.CLOCK, INF),
    )
    assert timeline == (
        iv(AWAY, _t(13, 8, 0), _t(13, 12, 0)),
        iv(ASLEEP, _t(13, 12, 0), _t(13, 13, 0), Source.CLOCK, INF),
        iv(AWAY, _t(13, 13, 0), None, Source.CLOCK),
    )


# A clock jump's boundaries sit on collector ticks: observed on the awake side, inferred on the sleeping side.


def test_a_clock_only_sleep_in_the_middle_of_presence_is_inferred_only_while_asleep():
    # The suspend is the last tick before the jump and the resume the first tick after it; only the stretch between them is inferred.
    timeline = intervals(
        start(_t(19, 8, 0)),
        unlock(_t(19, 8, 1), Source.WTS),
        suspend(_t(19, 10, 0), Source.CLOCK, INF),
        resume(_t(19, 10, 45), Source.CLOCK, INF),
        lock(_t(19, 11, 30), Source.WTS),
    )
    assert timeline == (
        iv(UNEVIDENCED, _t(19, 8, 0), _t(19, 8, 1), Source.COLLECTOR),
        iv(PRESENT, _t(19, 8, 1), _t(19, 10, 0), Source.WTS),
        iv(ASLEEP, _t(19, 10, 0), _t(19, 10, 45), Source.CLOCK, INF),
        iv(UNEVIDENCED, _t(19, 10, 45), _t(19, 11, 30), Source.CLOCK),
        iv(AWAY, _t(19, 11, 30), None, Source.WTS),
    )


def test_observed_presence_on_both_sides_of_a_short_clock_sleep_stays_two_intervals():
    timeline = intervals(
        start(_t(19, 8, 0)),
        unlock(_t(19, 8, 0)),
        suspend(_t(19, 10, 0), Source.CLOCK, INF),
        resume(_t(19, 10, 2), Source.CLOCK, INF),
        user_input(_t(19, 10, 2)),
    )
    assert timeline == (
        iv(PRESENT, _t(19, 8, 0), _t(19, 10, 0)),
        iv(ASLEEP, _t(19, 10, 0), _t(19, 10, 2), Source.CLOCK, INF),
        iv(PRESENT, _t(19, 10, 2), None, Source.WIN32),
    )


def test_a_clock_pair_later_paired_with_eventlog_records_takes_their_times():
    # The System records were appended after the clock pair; replay pairs them by sequence and the eventlog times win. The 506 is before the last tick, as under Modern Standby.
    timeline = intervals(
        start(_t(19, 8, 0)),
        unlock(_t(19, 8, 0)),
        suspend(_t(19, 9, 58)),
        suspend(_t(19, 10, 0), Source.CLOCK, INF),
        resume(_t(19, 10, 44)),
        resume(_t(19, 10, 45), Source.CLOCK, INF),
    )
    assert timeline == (
        iv(PRESENT, _t(19, 8, 0), _t(19, 9, 58)),
        iv(ASLEEP, _t(19, 9, 58), _t(19, 10, 44)),
        iv(UNEVIDENCED, _t(19, 10, 44), None),
    )


def test_a_phase_2_sleep_in_the_middle_of_presence_is_inferred_only_while_asleep():
    # The phase 2 pair has the same tick semantics: the suspended stop at the last tick before the jump, the resumed start at the first tick after it.
    timeline = intervals(
        start(_t(19, 8, 0)),
        unlock(_t(19, 8, 1)),
        stop(_t(19, 10, 0), "suspended", INF),
        start(_t(19, 10, 45), "resumed"),
        lock(_t(19, 11, 30)),
    )
    assert timeline == (
        iv(UNEVIDENCED, _t(19, 8, 0), _t(19, 8, 1), Source.COLLECTOR),
        iv(PRESENT, _t(19, 8, 1), _t(19, 10, 0)),
        iv(ASLEEP, _t(19, 10, 0), _t(19, 10, 45), Source.COLLECTOR, INF),
        iv(UNEVIDENCED, _t(19, 10, 45), _t(19, 11, 30), Source.COLLECTOR),
        iv(AWAY, _t(19, 11, 30), None),
    )


# Gaps between runs (roadmap section 8): reconstruction, `off`, folding, downtime.


def test_a_gap_both_logs_cover_carries_the_state_through_and_applies_the_records_inside():
    gap_start, gap_end = _t(13, 10, 0), _t(13, 10, 20)
    assert intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        stop(gap_start, "unclean", INF),
        lock(_t(13, 10, 5)),
        start(gap_end),
        covered(gap_start, gap_end),
    ) == (
        iv(PRESENT, _t(13, 8, 0), gap_start),
        iv(PRESENT, gap_start, _t(13, 10, 5), Source.COLLECTOR, INF),
        iv(AWAY, _t(13, 10, 5), gap_end, Source.EVENTLOG, INF),
        iv(AWAY, gap_end, None, Source.COLLECTOR),
    )


def test_every_interval_inside_a_reconstructed_gap_is_inferred_and_its_edges_split_the_interval():
    gap_start, gap_end = _t(13, 10, 0), _t(13, 10, 20)
    assert intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        stop(gap_start, "session_end"),
        start(gap_end),
        covered(gap_start, gap_end),
    ) == (
        iv(PRESENT, _t(13, 8, 0), gap_start),
        iv(PRESENT, gap_start, gap_end, Source.COLLECTOR, INF),
        iv(PRESENT, gap_end, None, Source.COLLECTOR),
    )


def test_a_probe_unlock_at_start_pairs_with_the_backfilled_unlock_inside_a_reconstructed_gap():
    gap_start, gap_end = _t(13, 9, 0), _t(13, 9, 10)
    timeline = intervals(
        start(_t(13, 8, 0)),
        lock(_t(13, 8, 30)),
        stop(gap_start, "unclean", INF),
        unlock(_t(13, 9, 5)),
        start(gap_end),
        unlock(gap_end, Source.PROBE, INF),
        covered(gap_start, gap_end),
    )
    assert timeline[-4:] == (
        iv(AWAY, _t(13, 8, 30), gap_start),
        iv(AWAY, gap_start, _t(13, 9, 5), Source.COLLECTOR, INF),
        iv(PRESENT, _t(13, 9, 5), gap_end, Source.EVENTLOG, INF),
        iv(PRESENT, gap_end, None, Source.COLLECTOR),
    )


def test_a_sleep_carries_through_a_reconstructed_gap_to_its_resume():
    gap_start, gap_end = _t(13, 9, 5), _t(13, 9, 40)
    timeline = intervals(
        start(_t(13, 8, 0)),
        lock(_t(13, 8, 0)),
        suspend(_t(13, 9, 0)),
        stop(gap_start, "unclean", INF),
        resume(_t(13, 9, 30)),
        start(gap_end),
        covered(gap_start, gap_end),
    )
    assert timeline[-4:] == (
        iv(ASLEEP, _t(13, 9, 0), gap_start),
        iv(ASLEEP, gap_start, _t(13, 9, 30), Source.COLLECTOR, INF),
        iv(AWAY, _t(13, 9, 30), gap_end, Source.EVENTLOG, INF),
        iv(AWAY, gap_end, None, Source.COLLECTOR),
    )


def test_a_reboot_inside_a_reconstructed_gap_is_off_then_away_until_the_logon():
    gap_start, gap_end = _t(13, 17, 0), _t(13, 17, 4)
    assert intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        stop(gap_start, "session_end"),
        lock(_t(13, 17, 0, 10), detail="Winlogon/Operational 811 Sens 3"),
        boot(_t(13, 17, 0, 20), "stop"),
        boot(_t(13, 17, 2), "start"),
        unlock(_t(13, 17, 3)),
        start(gap_end),
        covered(gap_start, gap_end),
    ) == (
        iv(PRESENT, _t(13, 8, 0), gap_start),
        iv(PRESENT, gap_start, _t(13, 17, 0, 10), Source.COLLECTOR, INF),
        iv(AWAY, _t(13, 17, 0, 10), _t(13, 17, 0, 20), Source.EVENTLOG, INF),
        iv(OFF, _t(13, 17, 0, 20), _t(13, 17, 2), Source.EVENTLOG, INF),
        iv(AWAY, _t(13, 17, 2), _t(13, 17, 3), Source.EVENTLOG, INF),
        iv(PRESENT, _t(13, 17, 3), gap_end, Source.EVENTLOG, INF),
        iv(PRESENT, gap_end, None, Source.COLLECTOR),
    )


def test_a_boot_with_no_shutdown_record_in_a_reconstructed_gap_is_downtime_up_to_it():
    # A covered gap holding only a Kernel-General 12 and an EventLog 6005, then the logon: the machine went down at a time no record shows.
    gap_start, gap_end = _t(13, 10, 0), _t(13, 10, 20)
    booted, logon = _t(13, 10, 12), _t(13, 10, 15)
    assert intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        stop(gap_start, "unclean", INF),
        boot(booted, "start", detail="System Microsoft-Windows-Kernel-General 12"),
        boot(booted + timedelta(seconds=4), "start"),
        unlock(logon),
        start(gap_end),
        covered(gap_start, gap_end),
    ) == (
        iv(PRESENT, _t(13, 8, 0), gap_start),
        iv(DOWNTIME, gap_start, booted, Source.COLLECTOR, INF),
        iv(AWAY, booted, logon, Source.EVENTLOG, INF),
        iv(PRESENT, logon, gap_end, Source.EVENTLOG, INF),
        iv(PRESENT, gap_end, None, Source.COLLECTOR),
    )


def test_a_boot_with_no_shutdown_record_is_downtime_from_the_last_record_before_it():
    gap_start, gap_end = _t(13, 17, 0), _t(13, 18, 2)
    assert intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        stop(gap_start, "session_end"),
        lock(_t(13, 17, 5)),
        boot(_t(13, 18, 0), "start"),
        unlock(_t(13, 18, 1)),
        start(gap_end),
        covered(gap_start, gap_end),
    ) == (
        iv(PRESENT, _t(13, 8, 0), gap_start),
        iv(PRESENT, gap_start, _t(13, 17, 5), Source.COLLECTOR, INF),
        iv(DOWNTIME, _t(13, 17, 5), _t(13, 18, 0), Source.EVENTLOG, INF),
        iv(AWAY, _t(13, 18, 0), _t(13, 18, 1), Source.EVENTLOG, INF),
        iv(PRESENT, _t(13, 18, 1), gap_end, Source.EVENTLOG, INF),
        iv(PRESENT, gap_end, None, Source.COLLECTOR),
    )


@pytest.mark.parametrize(
    "flag", ["system_readable", "system_reaches_back", "winlogon_readable", "winlogon_reaches_back"]
)
def test_a_partly_covered_gap_is_downtime(flag):
    gap_start, gap_end = _t(13, 10, 0), _t(13, 10, 20)
    timeline = intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        stop(gap_start, "unclean", INF),
        lock(_t(13, 10, 5)),
        start(gap_end),
        covered(gap_start, gap_end, **{flag: False}),
    )
    assert timeline[1:] == (
        iv(DOWNTIME, gap_start, gap_end, Source.COLLECTOR, INF),
        iv(AWAY, gap_end, None, Source.COLLECTOR),
    )


def test_an_unreadable_coverage_note_leaves_the_gap_downtime():
    gap_start, gap_end = _t(13, 10, 0), _t(13, 10, 20)
    note = _event(
        gap_end,
        EventType.NOTE,
        Source.COLLECTOR,
        kind="coverage",
        span_start=gap_start.isoformat(),
        span_end=gap_end.isoformat(),
        system_readable="true",
    )
    timeline = intervals(start(_t(13, 8, 0)), stop(gap_start, "unclean", INF), start(gap_end), note)
    assert [interval.kind for interval in timeline] == [UNEVIDENCED, DOWNTIME, UNEVIDENCED]


def test_a_coverage_note_for_a_shorter_span_does_not_cover_the_gap():
    gap_start, gap_end = _t(13, 10, 0), _t(13, 10, 20)
    timeline = intervals(
        start(_t(13, 8, 0)),
        stop(gap_start, "unclean", INF),
        start(gap_end),
        covered(gap_start + timedelta(minutes=1), gap_end),
    )
    assert [interval.kind for interval in timeline] == [UNEVIDENCED, DOWNTIME, UNEVIDENCED]


@pytest.mark.parametrize("reason", ["stop_file", "signal", "console_close", "max_ticks"])
def test_a_deliberate_gap_is_never_reconstructed_or_folded(reason):
    gap_start, gap_end = _t(13, 9, 0), _t(13, 9, 5)
    timeline = intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        stop(gap_start, reason),
        unlock(_t(13, 9, 1)),
        boot(_t(13, 9, 2), "start", Source.COLLECTOR),
        start(gap_end),
        covered(gap_start, gap_end),
        micro_max=timedelta(minutes=10),
    )
    assert timeline == (
        iv(PRESENT, _t(13, 8, 0), gap_start),
        iv(DOWNTIME, gap_start, gap_end, Source.COLLECTOR),
        iv(PRESENT, gap_end, None, Source.COLLECTOR),
    )


def test_an_uncovered_gap_with_the_boot_time_line_is_off_up_to_it_then_away_until_an_unlock():
    # micro_max plays no part: after any boot start the machine shows the sign-in screen.
    assert intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        stop(_t(13, 10, 0), "unclean", INF),
        boot(_t(13, 10, 30), "start", Source.COLLECTOR),
        unlock(_t(13, 10, 40)),
        start(_t(13, 11, 0)),
    ) == (
        iv(PRESENT, _t(13, 8, 0), _t(13, 10, 0)),
        iv(OFF, _t(13, 10, 0), _t(13, 10, 30), Source.COLLECTOR, INF),
        iv(AWAY, _t(13, 10, 30), _t(13, 10, 40), Source.COLLECTOR, INF),
        iv(PRESENT, _t(13, 10, 40), _t(13, 11, 0), Source.EVENTLOG, INF),
        iv(PRESENT, _t(13, 11, 0), None, Source.COLLECTOR),
    )


def test_an_eventlog_boot_record_in_an_uncovered_gap_is_not_the_boot_time_rule():
    timeline = intervals(
        start(_t(13, 8, 0)),
        stop(_t(13, 10, 0), "unclean", INF),
        boot(_t(13, 10, 30), "start"),
        start(_t(13, 11, 0)),
    )
    assert timeline[1] == iv(DOWNTIME, _t(13, 10, 0), _t(13, 11, 0), Source.COLLECTOR, INF)


@pytest.mark.parametrize(
    ("length", "micro_max", "folded"),
    [
        (timedelta(minutes=9, seconds=59), timedelta(minutes=10), True),
        (timedelta(minutes=10), timedelta(minutes=10), False),
        (timedelta(minutes=1), timedelta(), False),
    ],
)
def test_an_uncovered_gap_shorter_than_micro_max_folds_carrying_the_state(length, micro_max, folded):
    gap_start = _t(13, 10, 0)
    gap_end = gap_start + length
    timeline = intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        stop(gap_start, "unclean", INF),
        start(gap_end),
        micro_max=micro_max,
    )
    if folded:
        # The session carried through the fold is unverified, so the run after it stays inferred and merges with the fold (step 9b).
        assert timeline == (
            iv(PRESENT, _t(13, 8, 0), gap_start),
            iv(PRESENT, gap_start, None, Source.COLLECTOR, INF),
        )
    else:
        assert [interval.kind for interval in timeline] == [PRESENT, DOWNTIME, UNEVIDENCED]


def test_a_further_stop_inside_a_reconstructed_gap_does_not_end_it():
    gap_start, gap_end = _t(13, 10, 0), _t(13, 10, 20)
    timeline = intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        stop(gap_start, "session_end"),
        stop(_t(13, 10, 5), "unclean", INF),
        start(gap_end),
        covered(gap_start, gap_end),
    )
    assert DOWNTIME not in [interval.kind for interval in timeline]
    assert timeline[-1] == iv(PRESENT, gap_end, None, Source.COLLECTOR)


def test_a_gap_still_open_is_downtime_whatever_micro_max_says():
    timeline = intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        stop(_t(13, 10, 0), "unclean", INF),
        micro_max=timedelta(hours=1),
    )
    assert timeline[-1] == iv(DOWNTIME, _t(13, 10, 0), None, Source.COLLECTOR, INF)


# Phase 2 legacy shapes.


def test_legacy_liveness_with_no_session_events_is_unevidenced_never_present():
    assert intervals(start(_t(13, 8, 0)), stop(_t(13, 17, 0), "stop_file")) == (
        iv(UNEVIDENCED, _t(13, 8, 0), _t(13, 17, 0), Source.COLLECTOR),
        iv(DOWNTIME, _t(13, 17, 0), None, Source.COLLECTOR),
    )


def test_legacy_suspended_and_resumed_read_as_an_inferred_sleep():
    assert intervals(
        start(_t(13, 8, 0)),
        stop(_t(13, 12, 0), "suspended", INF),
        start(_t(13, 12, 40), "resumed"),
    ) == (
        iv(UNEVIDENCED, _t(13, 8, 0), _t(13, 12, 0), Source.COLLECTOR),
        iv(ASLEEP, _t(13, 12, 0), _t(13, 12, 40), Source.COLLECTOR, INF),
        iv(UNEVIDENCED, _t(13, 12, 40), None, Source.COLLECTOR),
    )


def test_legacy_suspended_followed_by_a_launch_start_stays_asleep_until_the_launch():
    assert intervals(
        start(_t(13, 8, 0)),
        stop(_t(13, 12, 0), "suspended", INF),
        start(_t(13, 15, 0)),
    )[1:] == (
        iv(ASLEEP, _t(13, 12, 0), _t(13, 15, 0), Source.COLLECTOR, INF),
        iv(UNEVIDENCED, _t(13, 15, 0), None, Source.COLLECTOR),
    )


def test_legacy_start_after_start_continues_observing():
    assert intervals(start(_t(13, 8, 0)), start(_t(13, 9, 0)), stop(_t(13, 10, 0), "signal")) == (
        iv(UNEVIDENCED, _t(13, 8, 0), _t(13, 10, 0), Source.COLLECTOR),
        iv(DOWNTIME, _t(13, 10, 0), None, Source.COLLECTOR),
    )


def test_events_before_the_first_start_produce_no_interval():
    assert intervals(stop(_t(13, 7, 0), "signal"), lock(_t(13, 7, 30)), start(_t(13, 8, 0))) == (
        iv(AWAY, _t(13, 8, 0), None, Source.COLLECTOR),
    )


def test_a_ledger_with_no_start_has_no_intervals():
    assert intervals(stop(_t(13, 7, 0), "signal"), unlock(_t(13, 7, 30))) == ()


# Shape of the timeline.


def test_zero_length_intervals_are_dropped_and_their_neighbours_merge():
    # The unlock and lock at the same instant leave no interval, so the two away spans join.
    assert intervals(
        start(_t(13, 8, 0)),
        lock(_t(13, 8, 0)),
        unlock(_t(13, 9, 0)),
        lock(_t(13, 9, 0)),
    ) == (iv(AWAY, _t(13, 8, 0), None),)


def test_adjacent_intervals_of_different_confidence_do_not_merge():
    # A probe unlock and a lock at the same instant: the zero-length present between them is dropped, and the away on either side differs in confidence.
    timeline = intervals(
        start(_t(13, 8, 0)),
        lock(_t(13, 8, 0)),
        unlock(_t(13, 9, 0), Source.PROBE, INF),
        lock(_t(13, 9, 0)),
    )
    assert timeline == (
        iv(AWAY, _t(13, 8, 0), _t(13, 9, 0), Source.EVENTLOG, INF),
        iv(AWAY, _t(13, 9, 0), None),
    )


def test_hosts_are_kept_separate_and_sorted_by_start():
    other = "desktop"
    timeline = intervals(
        start(_t(13, 8, 0)),
        start(_t(13, 8, 30), host=other),
        unlock(_t(13, 9, 0), host=other),
    )
    assert timeline == (
        iv(UNEVIDENCED, _t(13, 8, 0), None, Source.COLLECTOR),
        iv(UNEVIDENCED, _t(13, 8, 30), _t(13, 9, 0), Source.COLLECTOR, host=other),
        iv(PRESENT, _t(13, 9, 0), None, host=other),
    )


def test_intervals_never_overlap_and_are_stored_in_utc():
    timeline = intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        lock(_t(13, 10, 0), Source.WTS),
        lock(_t(13, 9, 59, 58)),
        suspend(_t(13, 10, 5)),
        resume(_t(13, 11, 0)),
        unlock(_t(13, 11, 1)),
    )
    for earlier, later in pairwise(timeline):
        assert earlier.end == later.start
    assert all(interval.start.tzinfo is UTC for interval in timeline)


# Same-timestamp order (phase 3 step 7b): `events.order_key` at both sort sites. Ids are forced, so the test sets the id order rather than the clock.


def with_id(event: Event, n: int) -> Event:
    return dataclasses.replace(event, id=f"{n:032x}")


_ID_ORDERS = [(1, 2), (2, 1)]


@pytest.mark.parametrize(("input_id", "resume_id"), _ID_ORDERS, ids=["input-id-first", "resume-id-first"])
def test_an_input_at_the_time_of_its_resume_opens_presence_whatever_the_ids_say(input_id, resume_id):
    wake = _t(13, 12, 3)
    timeline = intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        suspend(_t(13, 12, 0)),
        with_id(resume(wake), resume_id),
        with_id(user_input(wake, Source.EVENTLOG), input_id),
    )
    assert timeline[-2:] == (iv(ASLEEP, _t(13, 12, 0), wake), iv(PRESENT, wake, None))


@pytest.mark.parametrize(("input_id", "resume_id"), _ID_ORDERS, ids=["input-id-first", "resume-id-first"])
def test_an_input_after_its_resume_opens_presence_at_its_own_time_as_before(input_id, resume_id):
    """The control: at different times the ids never mattered, and still do not."""
    wake, touched = _t(13, 12, 3), _t(13, 12, 4)
    timeline = intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        suspend(_t(13, 12, 0)),
        with_id(resume(wake), resume_id),
        with_id(user_input(touched, Source.EVENTLOG), input_id),
    )
    assert timeline[-2:] == (iv(UNEVIDENCED, wake, touched), iv(PRESENT, touched, None))


@pytest.mark.parametrize(("lock_id", "unlock_id"), _ID_ORDERS, ids=["lock-id-first", "unlock-id-first"])
def test_a_lock_and_an_unlock_at_one_time_read_locked_from_then_on(lock_id, unlock_id):
    tie = _t(13, 9, 0)
    timeline = intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        with_id(lock(tie), lock_id),
        with_id(unlock(tie), unlock_id),
    )
    assert timeline == (iv(PRESENT, _t(13, 8, 0), tie), iv(AWAY, tie, None))


@pytest.mark.parametrize(("input_id", "other_id"), _ID_ORDERS, ids=["input-id-first", "input-id-last"])
@pytest.mark.parametrize("closer", ["suspend", "stop"])
def test_an_input_at_the_time_of_a_suspend_or_a_stop_opens_nothing(closer, input_id, other_id):
    """Cover, not a change: the input sorts after the suspend or stop and is dropped; before it, it would open a zero-length presence the closer ends at once."""
    at = _t(13, 11, 0)
    closing = suspend(at) if closer == "suspend" else stop(at, "unclean", INF)
    timeline = intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        suspend(_t(13, 9, 0)),
        resume(_t(13, 10, 0)),
        with_id(closing, other_id),
        with_id(user_input(at, Source.EVENTLOG), input_id),
        resume(_t(13, 12, 0)) if closer == "suspend" else start(_t(13, 12, 0)),
    )
    after = ASLEEP if closer == "suspend" else DOWNTIME
    assert [interval.kind for interval in timeline] == [PRESENT, ASLEEP, UNEVIDENCED, after, UNEVIDENCED]
    assert timeline[3].start == at


@pytest.mark.parametrize("unlock_first", [True, False], ids=["unlock-id-first", "stop-id-first"])
def test_an_unlock_at_the_time_of_a_stop_stays_in_id_order(unlock_first):
    """Not ranked: the ties the collector writes keep today's id order (see `events.tie_rank`). An unlock before the stop is closed by it; one after it is inside the gap and opens presence from the next start."""
    at, restart = _t(13, 10, 0), _t(13, 11, 0)
    unlock_id, stop_id = (1, 2) if unlock_first else (2, 1)
    timeline = intervals(
        start(_t(13, 8, 0)),
        lock(_t(13, 8, 0)),
        with_id(unlock(at), unlock_id),
        with_id(stop(at, "unclean", INF), stop_id),
        start(restart),
    )
    kind = UNEVIDENCED if unlock_first else PRESENT
    assert timeline[-1] == iv(kind, restart, None, Source.COLLECTOR)


@pytest.mark.parametrize("start_first", [True, False], ids=["start-id-first", "unlock-id-first"])
def test_the_startup_probes_unlock_at_the_start_stays_in_id_order(start_first):
    """Not ranked: the collector writes the start before the probe's unlock, and the id order keeps the presence it opens from the probe. The reverse order opens it from the start; since step 9b the probe's inferred session fact carries into that interval, so both orders read inferred and only the source follows the id order."""
    restart = _t(13, 10, 20)
    start_id, unlock_id = (1, 2) if start_first else (2, 1)
    timeline = intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        stop(_t(13, 10, 0), "unclean", INF),
        with_id(start(restart), start_id),
        with_id(unlock(restart, Source.PROBE, INF), unlock_id),
    )
    expected = (
        iv(PRESENT, restart, None, Source.PROBE, INF)
        if start_first
        else iv(PRESENT, restart, None, Source.COLLECTOR, INF)
    )
    assert timeline[-1] == expected


def test_the_boundaries_sort_by_rank_before_id():
    """The second sort site, `_boundaries`: at one time a resume comes before its input and an unlock before a lock, whatever the ids say."""
    wake, tie = _t(13, 12, 3), _t(13, 13, 0)
    events = [
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        lock(_t(13, 8, 30)),
        suspend(_t(13, 12, 0)),
        with_id(resume(wake), 4),
        with_id(user_input(wake, Source.EVENTLOG), 3),
        with_id(lock(tie), 1),
        with_id(unlock(tie), 2),
    ]
    steps = [(b.ts, b.step) for b in _boundaries(sorted(events, key=order_key), timedelta())]
    assert steps[-4:] == [(wake, _Step.RESUME), (wake, _Step.INPUT), (tie, _Step.UNLOCK), (tie, _Step.LOCK)]


@pytest.mark.parametrize(("input_id", "resume_id"), _ID_ORDERS, ids=["input-id-first", "resume-id-first"])
def test_the_timelines_events_put_a_resume_before_its_input_and_an_unlock_before_a_lock(input_id, resume_id):
    """The first sort site, `replay()` itself: the events it hands back are in `order_key` order."""
    wake, tie = _t(13, 12, 3), _t(13, 13, 0)
    timeline = replay(
        [
            start(_t(13, 8, 0)),
            unlock(_t(13, 8, 0)),
            suspend(_t(13, 12, 0)),
            with_id(resume(wake), resume_id),
            with_id(user_input(wake, Source.EVENTLOG), input_id),
            with_id(lock(tie), input_id),
            with_id(unlock(tie), resume_id),
        ]
    )
    kinds = [(event.ts, event.event) for event in timeline.events]
    assert kinds[-4:] == [
        (wake, EventType.RESUME),
        (wake, EventType.INPUT),
        (tie, EventType.UNLOCK),
        (tie, EventType.LOCK),
    ]
