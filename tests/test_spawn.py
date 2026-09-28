import json
import os
import subprocess

import pytest

from timeexisting import paths
from timeexisting.collector import lockfile, spawn
from timeexisting.collector.spawn import spawn as real_spawn

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


def test_ensure_running_returns_a_live_collector_without_spawning():
    lock = lockfile.acquire("collector")
    owner = spawn.ensure_running(
        spawner=lambda: pytest.fail("must not spawn"), sleep=lambda _: pytest.fail("must not wait")
    )
    assert owner == lock.status


def test_ensure_running_spawns_and_returns_the_owner_once_the_lock_appears():
    spawned: list[bool] = []
    waited: list[float] = []
    held: list[lockfile.Lock] = []

    def sleep(seconds: float) -> None:
        waited.append(seconds)
        if len(waited) == 2:
            held.append(lockfile.acquire("collector"))

    owner = spawn.ensure_running(spawner=lambda: spawned.append(True), sleep=sleep)

    assert spawned == [True]
    assert len(waited) == 2
    assert owner == held[0].status


def test_ensure_running_gives_up_after_the_timeout():
    waited: list[float] = []
    owner = spawn.ensure_running(spawner=lambda: None, sleep=waited.append)
    assert owner is None
    assert sum(waited) == pytest.approx(spawn.SPAWN_TIMEOUT)


def test_ensure_running_survives_a_spawn_that_raises():
    def spawner() -> None:
        raise FileNotFoundError("pythonw.exe")

    owner = spawn.ensure_running(spawner=spawner, sleep=lambda _: pytest.fail("must not wait"))
    assert owner is None


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

    owner = spawn.ensure_running(spawner=lambda: spawned.append(True), sleep=lambda _: None)

    assert spawned == [True]
    assert owner is None
    assert stale.exists(), "reclaiming the stale lock is the spawned collector's job"
