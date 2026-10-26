"""Step 9b: the confidence a probe boundary, a folded gap and the startup probe's lock give the intervals around them.

Rule A: a probe transition is written at the first of two disagreeing ticks, so the real change lies in the tick before it; with `collector.tick` that tick is inferred and the rest of the interval keeps its confidence. Rule B: a session fact that rests on an inference, a probe transition or a known session carried through a fold, keeps the intervals built on it inferred until an observed transition replaces it. The startup probe writes an inferred lock for a locked session, so a fold cannot carry presence past a start on a locked session.
"""

from datetime import timedelta

from tests.timeline_helpers import (
    ASLEEP,
    AWAY,
    DOWNTIME,
    INF,
    OBS,
    PRESENT,
    UNEVIDENCED,
    _t,
    covered,
    intervals,
    iv,
    lock,
    resume,
    start,
    stop,
    suspend,
    unlock,
    user_input,
)
from timeexisting.ledger.events import Source
from timeexisting.ledger.replay import replay

TICK = timedelta(seconds=30)
MICRO_MAX = timedelta(minutes=10)


def ticked(*events, micro_max: timedelta = MICRO_MAX, tick: timedelta = TICK):
    return replay(events, micro_max=micro_max, tick=tick).intervals


# --- B: the session fact's confidence -------------------------------------


def test_b_the_covered_26_october_shape_reads_observed_after_the_restart():
    """26 October on the dev ledger: an unclean stop the evening before, the gap reconstructed from both logs with a 507 at 08:14:54 and an 811 unlock at 08:15:08 inside it, then `collector_start` at 09:20:58 with the startup probe's inferred `unlock` at the same second. The unlock inside the covered gap is an observed record with no lock after it, so the session fact the run inherits is observed and the probe only confirms it: the run reads observed, as 6b pinned. Decided at the step 9b review."""
    gap_start, gap_end = _t(25, 17, 0), _t(26, 9, 20, 58)
    timeline = intervals(
        start(_t(25, 8, 0)),
        unlock(_t(25, 8, 0)),
        lock(_t(25, 16, 0)),
        stop(gap_start, "unclean", INF),
        resume(_t(26, 8, 14, 54)),
        unlock(_t(26, 8, 15, 8)),
        start(gap_end),
        unlock(gap_end, Source.PROBE, INF),
        covered(gap_start, gap_end),
        micro_max=MICRO_MAX,
    )
    assert timeline[-2:] == (
        iv(PRESENT, _t(26, 8, 15, 8), gap_end, Source.EVENTLOG, INF),
        iv(PRESENT, gap_end, None, Source.COLLECTOR, OBS),
    )


def test_b_after_a_folded_gap_the_restart_rests_on_the_probe_alone():
    """Presence observed up to an unclean stop, a five-minute uncovered gap folded, then a restart whose only session evidence is the startup probe's inferred unlock. The carried session is unverified, so the run stays inferred and merges with the fold, until an observed lock replaces the fact."""
    gap_start, gap_end = _t(26, 8, 30), _t(26, 8, 35)
    timeline = intervals(
        start(_t(26, 8, 0)),
        unlock(_t(26, 8, 0)),
        stop(gap_start, "unclean", INF),
        start(gap_end),
        unlock(gap_end, Source.PROBE, INF),
        lock(_t(26, 10, 0)),
        micro_max=MICRO_MAX,
    )
    assert timeline == (
        iv(PRESENT, _t(26, 8, 0), gap_start),
        iv(PRESENT, gap_start, _t(26, 10, 0), Source.COLLECTOR, INF),
        iv(AWAY, _t(26, 10, 0), None),
    )


def test_b_a_fold_of_an_unknown_session_changes_nothing():
    """The prod ledger's shape: phase 2 runs with no session event, an unclean stop and a restart six minutes later. The fold carries an unknown session, which has nothing to infer, so the legacy unevidenced run after it stays observed."""
    gap_start, gap_end = _t(5, 10, 14, 55), _t(5, 10, 21, 0)
    timeline = intervals(
        start(_t(5, 8, 0)),
        stop(gap_start, "unclean", INF),
        start(gap_end),
        stop(_t(5, 11, 8, 3), "suspended", INF),
        micro_max=MICRO_MAX,
    )
    assert timeline[:3] == (
        iv(UNEVIDENCED, _t(5, 8, 0), gap_start, Source.COLLECTOR),
        iv(UNEVIDENCED, gap_start, gap_end, Source.COLLECTOR, INF),
        iv(UNEVIDENCED, gap_end, _t(5, 11, 8, 3), Source.COLLECTOR),
    )


def test_b_a_probe_opened_session_stays_inferred_across_a_sleep_and_wake():
    """15 October on the dev ledger: a restart after a stop-file stop with the probe's unlock as the only session evidence, then a sleep and a 507 wake with no lock or unlock record. The session fact is still the probe's after the wake, so the unevidenced moment is inferred; an eventlog unlock after the wake makes it observed."""
    restart = _t(15, 14, 19, 7)
    probe_only = intervals(
        start(_t(15, 8, 0)),
        unlock(_t(15, 8, 0)),
        stop(_t(15, 14, 19, 0), "stop_file"),
        start(restart),
        unlock(restart, Source.PROBE, INF),
        suspend(_t(15, 16, 46, 2)),
        resume(_t(15, 16, 46, 5)),
        suspend(_t(15, 16, 46, 26)),
    )
    assert probe_only[-3:] == (
        iv(ASLEEP, _t(15, 16, 46, 2), _t(15, 16, 46, 5)),
        iv(UNEVIDENCED, _t(15, 16, 46, 5), _t(15, 16, 46, 26), Source.EVENTLOG, INF),
        iv(ASLEEP, _t(15, 16, 46, 26), None),
    )
    relocked = intervals(
        start(_t(15, 8, 0)),
        unlock(_t(15, 8, 0)),
        stop(_t(15, 14, 19, 0), "stop_file"),
        start(restart),
        unlock(restart, Source.PROBE, INF),
        lock(_t(15, 15, 0)),
        unlock(_t(15, 15, 5)),
        suspend(_t(15, 16, 46, 2)),
        resume(_t(15, 16, 46, 5)),
        suspend(_t(15, 16, 46, 26)),
    )
    assert relocked[-2] == iv(UNEVIDENCED, _t(15, 16, 46, 5), _t(15, 16, 46, 26), Source.EVENTLOG, OBS)


def test_b_an_observed_lock_after_a_probe_unlock_reads_observed():
    restart = _t(13, 10, 20)
    timeline = intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        stop(_t(13, 10, 0), "stop_file"),
        start(restart),
        unlock(restart, Source.PROBE, INF),
        lock(_t(13, 11, 0)),
        unlock(_t(13, 11, 30)),
    )
    assert timeline[-3:] == (
        iv(PRESENT, restart, _t(13, 11, 0), Source.PROBE, INF),
        iv(AWAY, _t(13, 11, 0), _t(13, 11, 30)),
        iv(PRESENT, _t(13, 11, 30), None),
    )


def test_b_input_on_a_probe_opened_session_stays_inferred_but_from_a_logon_is_observed():
    """Evidence opens presence, but the session it lands on is the probe's, so the presence is inferred; the same input on a session from a logon is observed."""
    restart = _t(13, 10, 20)
    events = (
        stop(_t(13, 10, 0), "stop_file"),
        start(restart),
        suspend(_t(13, 11, 0)),
        resume(_t(13, 11, 30)),
        user_input(_t(13, 11, 31)),
    )
    from_probe = intervals(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        *events[:2],
        unlock(restart, Source.PROBE, INF),
        *events[2:],
    )
    assert from_probe[-1] == iv(PRESENT, _t(13, 11, 31), None, Source.WIN32, INF)
    from_logon = intervals(
        start(_t(13, 8, 0)), unlock(_t(13, 8, 0)), *events[:2], unlock(restart, Source.LOGON), *events[2:]
    )
    assert from_logon[-1] == iv(PRESENT, _t(13, 11, 31), None, Source.WIN32, OBS)


# --- A: the tick before a probe boundary ----------------------------------


def test_a_a_probe_lock_makes_only_the_last_tick_before_it_inferred():
    """The 6c rule: presence observed through an eventlog unlock stays observed up to one tick before the probe lock, is inferred for that tick, and away from the probe lock on, inferred. The same lock from the event log splits nothing."""
    probe_lock = _t(26, 10, 0)
    timeline = ticked(start(_t(26, 8, 0)), unlock(_t(26, 8, 0)), lock(probe_lock, Source.PROBE, INF))
    assert timeline == (
        iv(PRESENT, _t(26, 8, 0), probe_lock - TICK),
        iv(PRESENT, probe_lock - TICK, probe_lock, Source.EVENTLOG, INF),
        iv(AWAY, probe_lock, None, Source.PROBE, INF),
    )
    logged = ticked(start(_t(26, 8, 0)), unlock(_t(26, 8, 0)), lock(probe_lock))
    assert logged == (iv(PRESENT, _t(26, 8, 0), probe_lock), iv(AWAY, probe_lock, None))


def test_a_a_probe_unlock_splits_the_away_interval_it_closes_the_same_way():
    probe_unlock = _t(26, 10, 0)
    timeline = ticked(
        start(_t(26, 8, 0)),
        unlock(_t(26, 8, 0)),
        lock(_t(26, 9, 0)),
        unlock(probe_unlock, Source.PROBE, INF),
    )
    assert timeline[-3:] == (
        iv(AWAY, _t(26, 9, 0), probe_unlock - TICK),
        iv(AWAY, probe_unlock - TICK, probe_unlock, Source.EVENTLOG, INF),
        iv(PRESENT, probe_unlock, None, Source.PROBE, INF),
    )


def test_a_a_probe_boundary_within_a_tick_of_the_intervals_start_makes_it_wholly_inferred():
    """13 October on the dev ledger: a false probe unlock three seconds after a WTS lock. The tick before the probe reaches past the interval's start, so the three-second away interval is inferred as a whole, as before."""
    probe_unlock = _t(13, 12, 23, 39)
    timeline = ticked(
        start(_t(13, 8, 0)),
        unlock(_t(13, 8, 0)),
        lock(_t(13, 12, 23, 36), Source.WTS),
        unlock(probe_unlock, Source.PROBE, INF),
    )
    assert timeline[-2:] == (
        iv(AWAY, _t(13, 12, 23, 36), probe_unlock, Source.WTS, INF),
        iv(PRESENT, probe_unlock, None, Source.PROBE, INF),
    )


def test_a_without_a_tick_the_whole_interval_a_probe_closes_is_inferred():
    """The default, which every caller now overrides with `collector.tick`: today's reading, kept for a replay that knows no tick."""
    probe_lock = _t(26, 10, 0)
    timeline = ticked(
        start(_t(26, 8, 0)), unlock(_t(26, 8, 0)), lock(probe_lock, Source.PROBE, INF), tick=timedelta()
    )
    assert timeline == (
        iv(PRESENT, _t(26, 8, 0), probe_lock, Source.EVENTLOG, INF),
        iv(AWAY, probe_lock, None, Source.PROBE, INF),
    )


def test_a_the_startup_probe_splits_nothing_before_the_start():
    """After a deliberate stop the gap is downtime; the startup probe's unlock at the restart opens presence from the restart and leaves the downtime as it was."""
    restart = _t(26, 10, 20)
    timeline = ticked(
        start(_t(26, 8, 0)),
        unlock(_t(26, 8, 0)),
        stop(_t(26, 10, 0), "stop_file"),
        start(restart),
        unlock(restart, Source.PROBE, INF),
    )
    assert timeline[-2:] == (
        iv(DOWNTIME, _t(26, 10, 0), restart, Source.COLLECTOR),
        iv(PRESENT, restart, None, Source.PROBE, INF),
    )


# --- The startup probe's lock ----------------------------------------------


def test_a_start_on_a_locked_session_after_a_fold_gives_no_presence_after_the_start():
    """A fold carried an unlocked session through the gap; the startup probe found the session locked and wrote its inferred lock, so presence ends at the restart. Control: the same restart on an unlocked session gives presence, inferred."""
    gap_start, gap_end = _t(26, 8, 30), _t(26, 8, 35)
    before_restart = (
        start(_t(26, 8, 0)),
        unlock(_t(26, 8, 0)),
        stop(gap_start, "unclean", INF),
        start(gap_end),
    )
    locked = intervals(*before_restart, lock(gap_end, Source.PROBE, INF), micro_max=MICRO_MAX)
    assert locked == (
        iv(PRESENT, _t(26, 8, 0), gap_start),
        iv(PRESENT, gap_start, gap_end, Source.COLLECTOR, INF),
        iv(AWAY, gap_end, None, Source.PROBE, INF),
    )
    unlocked = intervals(*before_restart, unlock(gap_end, Source.PROBE, INF), micro_max=MICRO_MAX)
    assert unlocked == (
        iv(PRESENT, _t(26, 8, 0), gap_start),
        iv(PRESENT, gap_start, None, Source.COLLECTOR, INF),
    )


def test_a_start_on_a_locked_session_after_a_clean_stop_reads_away_instead_of_unevidenced():
    """After a deliberate stop the session is unknown at the restart. Before the startup lock the run opened unevidenced, observed; with it the run opens away, inferred, from the probe. Neither gives presence."""
    restart = _t(26, 10, 20)
    before_restart = (
        start(_t(26, 8, 0)),
        unlock(_t(26, 8, 0)),
        stop(_t(26, 10, 0), "stop_file"),
        start(restart),
    )
    with_lock = intervals(*before_restart, lock(restart, Source.PROBE, INF))
    without = intervals(*before_restart)
    assert with_lock[:-1] == without[:-1]
    assert with_lock[-1] == iv(AWAY, restart, None, Source.PROBE, INF)
    assert without[-1] == iv(UNEVIDENCED, restart, None, Source.COLLECTOR)
    assert PRESENT not in [interval.kind for interval in with_lock[2:]]
    assert PRESENT not in [interval.kind for interval in without[2:]]
