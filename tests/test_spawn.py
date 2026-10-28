import json
import logging
import os
import subprocess
from datetime import UTC, datetime, timedelta

import pytest

from timeexisting import paths
from timeexisting.collector import daemon, lockfile, spawn
from timeexisting.collector.reconcile import ReadState
from timeexisting.collector.spawn import spawn as real_spawn

_TICK = datetime(2026, 10, 28, 7, 57, 45, tzinfo=UTC)


def _reconciled_checkpoint(ts: datetime = _TICK, pid: int | None = None) -> None:
    """A checkpoint as `daemon._loop` writes it after the first reconciliation: the read cursors set, for `pid` (ours by default)."""
    daemon.write_checkpoint(paths.current_host(), ts, None, ReadState(system_cursor=ts, winlogon_cursor=ts))
    if pid is not None:
        path = paths.checkpoint_file(paths.current_host())
        record = json.loads(path.read_text(encoding="utf-8"))
        record["pid"] = pid
        path.write_text(json.dumps(record), encoding="utf-8")


_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def _interpreter_dir(tmp_path, *, windowless: bool):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").touch()
    if windowless:
        (scripts / "pythonw.exe").touch()
    return scripts


def test_command_uses_pythonw_beside_the_interpreter_on_windows(tmp_path):
    scripts = _interpreter_dir(tmp_path, windowless=True)
    argv = spawn.command("win32", str(scripts / "python.exe"))
    assert argv == [str(scripts / "pythonw.exe"), "-m", "timeexisting", "collect"]


def test_command_falls_back_to_the_interpreter_without_pythonw(tmp_path):
    scripts = _interpreter_dir(tmp_path, windowless=False)
    argv = spawn.command("win32", str(scripts / "python.exe"))
    assert argv == [str(scripts / "python.exe"), "-m", "timeexisting", "collect"]


def test_command_uses_the_interpreter_elsewhere():
    assert spawn.command("linux", "/usr/bin/python3") == ["/usr/bin/python3", "-m", "timeexisting", "collect"]


def _record_popen():
    calls: list[tuple[list[str], dict]] = []

    def popen(argv, **options):
        calls.append((argv, options))

    return calls, popen


def test_spawn_on_windows_detaches_with_no_window_and_no_streams():
    calls, popen = _record_popen()
    real_spawn(platform="win32", popen=popen)

    [(argv, options)] = calls
    assert argv[1:] == ["-m", "timeexisting", "collect"]
    assert options["creationflags"] == _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
    assert "start_new_session" not in options
    for stream in ("stdin", "stdout", "stderr"):
        assert options[stream] is subprocess.DEVNULL


def test_spawn_elsewhere_starts_a_new_session_with_no_streams():
    calls, popen = _record_popen()
    real_spawn(platform="linux", popen=popen)

    [(argv, options)] = calls
    assert argv[1:] == ["-m", "timeexisting", "collect"]
    assert options["start_new_session"] is True
    assert "creationflags" not in options
    for stream in ("stdin", "stdout", "stderr"):
        assert options[stream] is subprocess.DEVNULL


def test_ensure_running_returns_a_live_collector_without_spawning_or_waiting(monkeypatch):
    """A collector found running is returned at once: no spawn, no sleep, no checkpoint read."""
    lock = lockfile.acquire("collector")
    monkeypatch.setattr(
        spawn, "reconciled_checkpoint", lambda *_: pytest.fail("must not read the checkpoint")
    )
    ensured = spawn.ensure_running(
        spawner=lambda: pytest.fail("must not spawn"), sleep=lambda _: pytest.fail("must not wait")
    )
    assert ensured == spawn.Ensured(lock.status, spawned=False)
    assert ensured.failed is False


def test_ensure_running_spawns_and_returns_the_owner_once_the_lock_and_its_reconciliation_appear():
    """The lock on the second poll, the reconciled checkpoint on the third: three waits, and the owner."""
    spawned: list[bool] = []
    waited: list[float] = []
    held: list[lockfile.Lock] = []

    def sleep(seconds: float) -> None:
        waited.append(seconds)
        if len(waited) == 2:
            held.append(lockfile.acquire("collector"))
        if len(waited) == 3:
            _reconciled_checkpoint()

    ensured = spawn.ensure_running(spawner=lambda: spawned.append(True), sleep=sleep)

    assert spawned == [True]
    assert len(waited) == 3
    assert ensured == spawn.Ensured(held[0].status, spawned=True)
    assert ensured.failed is False


def test_ensure_running_gives_up_after_the_timeout():
    waited: list[float] = []
    ensured = spawn.ensure_running(spawner=lambda: None, sleep=waited.append)
    assert ensured == spawn.Ensured(None, spawned=True)
    assert ensured.failed is True
    assert sum(waited) == pytest.approx(spawn.SPAWN_TIMEOUT)


def test_the_timeouts_cover_the_dev_hosts_interpreter_start_and_first_read():
    """At step 10 the detached collector took 3.0 to 3.9 s to its lock, so 3.0 s missed two launches of four; its first read of a 17-hour gap took 0.3 s."""
    assert spawn.SPAWN_TIMEOUT == 5.0
    assert spawn.SETTLE_TIMEOUT == 10.0


def test_ensure_running_survives_a_spawn_that_raises():
    def spawner() -> None:
        raise FileNotFoundError("pythonw.exe")

    ensured = spawn.ensure_running(spawner=spawner, sleep=lambda _: pytest.fail("must not wait"))
    assert ensured == spawn.Ensured(None, spawned=True)


def test_ensure_running_returns_the_owner_after_the_settle_timeout_with_a_warning(caplog):
    """The lock on the first poll and never a reconciled checkpoint: the viewer carries on after the settle timeout with the owner, logged."""
    waited: list[float] = []
    held: list[lockfile.Lock] = []

    def sleep(seconds: float) -> None:
        waited.append(seconds)
        if len(waited) == 1:
            held.append(lockfile.acquire("collector"))

    with caplog.at_level(logging.WARNING, logger=spawn.__name__):
        ensured = spawn.ensure_running(spawner=lambda: None, sleep=sleep, settle_timeout=2.0)

    assert ensured == spawn.Ensured(held[0].status, spawned=True)
    assert sum(waited) == pytest.approx(0.25 + 2.0)
    assert any("first reconciliation" in record.message for record in caplog.records)


@pytest.mark.parametrize("marker", ["first checkpoint", "another pid"], ids=["first-checkpoint", "other-pid"])
def test_ensure_running_does_not_settle_on_the_first_checkpoint_or_another_process(marker):
    """The checkpoint written right after `collector_start` holds no cursors, and a leftover from another process is not this run's: neither ends the wait; the reconciled one for the owner's pid does."""
    waited: list[float] = []

    def sleep(seconds: float) -> None:
        waited.append(seconds)
        if len(waited) == 1:
            lockfile.acquire("collector")
        if len(waited) == 2:
            if marker == "first checkpoint":
                daemon.write_checkpoint(paths.current_host(), _TICK)
            else:
                _reconciled_checkpoint(pid=os.getpid() + 1)
        if len(waited) == 4:
            _reconciled_checkpoint()

    ensured = spawn.ensure_running(spawner=lambda: None, sleep=sleep)
    assert ensured.owner is not None and ensured.spawned
    assert len(waited) == 4


def test_wait_for_reconciliation_reads_before_the_first_sleep():
    lock = lockfile.acquire("collector")
    _reconciled_checkpoint()
    stamp = spawn.wait_for_reconciliation(
        lock.status, timeout=1.0, sleep=lambda _: pytest.fail("must not wait")
    )
    assert stamp == _TICK


def test_wait_for_reconciliation_with_after_waits_for_a_later_tick():
    """`after` is the tick on disk: the same tick does not count, the next one does. Without `after`, the tick on disk counts at once."""
    lock = lockfile.acquire("collector")
    _reconciled_checkpoint(_TICK)
    waited: list[float] = []

    def sleep(seconds: float) -> None:
        waited.append(seconds)
        if len(waited) == 2:
            _reconciled_checkpoint(_TICK + timedelta(seconds=30))

    stamp = spawn.wait_for_reconciliation(lock.status, after=_TICK, timeout=5.0, sleep=sleep)
    assert stamp == _TICK + timedelta(seconds=30)
    assert len(waited) == 2
    assert spawn.wait_for_reconciliation(lock.status, timeout=5.0, sleep=waited.append) == stamp
    assert len(waited) == 2


def test_wait_for_reconciliation_gives_up_after_the_timeout():
    lock = lockfile.acquire("collector")
    waited: list[float] = []
    assert spawn.wait_for_reconciliation(lock.status, timeout=1.0, sleep=waited.append) is None
    assert sum(waited) == pytest.approx(1.0)


def test_ensure_running_spawns_over_a_stale_lock():
    stale = paths.lock_file("collector")
    stale.parent.mkdir(parents=True, exist_ok=True)
    # Our own pid with a start time it never had: a recycled pid, stale.
    record = {
        "pid": os.getpid(),
        "host": paths.current_host(),
        "role": "collector",
        "create_time": 1.0,
        "schema": 1,
    }
    stale.write_text(json.dumps(record), encoding="utf-8")
    spawned: list[bool] = []

    ensured = spawn.ensure_running(spawner=lambda: spawned.append(True), sleep=lambda _: None)

    assert spawned == [True]
    assert ensured == spawn.Ensured(None, spawned=True)
    assert stale.exists(), "reclaiming the stale lock is the spawned collector's job"
