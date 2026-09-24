import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psutil
import pytest

from timeexisting import paths
from timeexisting.collector import lockfile
from timeexisting.content import phrases
from timeexisting.domain.clock import FixedClock
from timeexisting.paths import current_host

_HOLDER = """
import sys
from timeexisting.collector import lockfile
lock = lockfile.acquire("collector")
print(lock.status.pid, flush=True)
sys.stdin.read()
lockfile.release(lock)
"""


def _write_lock(role: str = "collector", **overrides) -> None:
    me = psutil.Process()
    record = {
        "pid": me.pid,
        "host": current_host(),
        "role": role,
        "create_time": me.create_time(),
        "schema": lockfile.LOCK_SCHEMA,
    } | overrides
    paths.lock_file(role).write_text(json.dumps(record), encoding="utf-8")


def _set_mtime(path, when: float) -> None:
    os.utime(path, (when, when))


def _dead_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


@dataclass(frozen=True)
class _Holder:
    process: subprocess.Popen
    pid: int


@pytest.fixture
def holder():
    """A second process holding the collector lock until its stdin closes. `pid` is the one the holder reports for itself: under a Windows venv, `sys.executable` is a launcher whose pid is not the interpreter's."""
    process = subprocess.Popen(
        [sys.executable, "-c", _HOLDER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        yield _Holder(process, int(process.stdout.readline()))
    finally:
        process.stdin.close()
        process.wait(timeout=10)
        process.stdout.close()


def test_acquire_writes_the_owner_record():
    lock = lockfile.acquire("collector")
    record = json.loads(paths.lock_file("collector").read_text(encoding="utf-8"))
    me = psutil.Process()
    assert record == {
        "pid": os.getpid(),
        "host": current_host(),
        "role": "collector",
        "create_time": me.create_time(),
        "schema": 1,
    }
    assert lock.path == paths.lock_file("collector")
    assert lock.status.started == datetime.fromtimestamp(me.create_time(), UTC)


def test_second_acquire_raises_already_running_with_pid_and_start_time():
    lock = lockfile.acquire("collector")
    with pytest.raises(lockfile.AlreadyRunning) as raised:
        lockfile.acquire("collector")
    assert raised.value.pid == os.getpid()
    assert raised.value.started == lock.status.started
    assert paths.lock_file("collector").exists()


def test_roles_are_independent():
    lockfile.acquire("collector")
    assert lockfile.acquire("viewer").status.role == "viewer"


def test_live_lock_in_another_process_is_refused(holder):
    with pytest.raises(lockfile.AlreadyRunning) as raised:
        lockfile.acquire("collector")
    assert raised.value.pid == holder.pid
    assert lockfile.status("collector").pid == holder.pid
    assert holder.pid != os.getpid()


def test_killed_holder_leaves_a_stale_lock_that_is_reclaimed(holder):
    interpreter = psutil.Process(holder.pid)
    interpreter.kill()
    interpreter.wait(timeout=10)
    assert paths.lock_file("collector").exists()
    assert lockfile.status("collector") is None
    assert lockfile.acquire("collector").status.pid == os.getpid()


def test_stale_pid_is_reclaimed():
    _write_lock(pid=_dead_pid())
    assert lockfile.acquire("collector").status.pid == os.getpid()


def test_recycled_pid_with_mismatched_create_time_is_stale():
    _write_lock(create_time=psutil.Process().create_time() - 100)
    assert lockfile.status("collector") is None
    assert lockfile.acquire("collector").status.pid == os.getpid()


def test_create_time_within_one_second_is_still_live():
    _write_lock(create_time=psutil.Process().create_time() - 0.5)
    with pytest.raises(lockfile.AlreadyRunning):
        lockfile.acquire("collector")


@pytest.mark.parametrize(
    "content",
    [
        "",
        "not json",
        "[1, 2]",
        '{"pid": 1}',
        '{"pid": "1", "host": "h", "role": "collector", "create_time": 1.0, "schema": 1}',
    ],
)
def test_old_unreadable_lock_is_stale(content):
    path = paths.lock_file("collector")
    path.write_text(content, encoding="utf-8")
    _set_mtime(path, time.time() - 10)
    assert lockfile.status("collector") is None
    assert lockfile.acquire("collector").status.pid == os.getpid()


def test_young_unreadable_lock_is_left_for_its_writer():
    path = paths.lock_file("collector")
    path.write_bytes(b"")
    _set_mtime(path, time.time() - 1)
    with pytest.raises(lockfile.LockError, match="still writing"):
        lockfile.acquire("collector")
    assert path.exists()
    assert path.read_bytes() == b""
    assert lockfile.status("collector") is None


def test_unreadable_grace_boundary():
    path = paths.lock_file("collector")
    path.write_bytes(b'{"pid": 1')
    written = datetime(2026, 9, 24, 8, 0, 0, tzinfo=UTC)
    _set_mtime(path, written.timestamp())

    with pytest.raises(lockfile.LockError):
        lockfile.acquire("collector", clock=FixedClock(written + timedelta(seconds=5)))
    assert path.read_bytes() == b'{"pid": 1'

    lock = lockfile.acquire("collector", clock=FixedClock(written + timedelta(seconds=5.5)))
    assert lock.status.pid == os.getpid()


def test_lock_from_another_host_is_never_reclaimed():
    _write_lock(pid=_dead_pid(), host="some-other-host")
    with pytest.raises(lockfile.AlreadyRunning) as raised:
        lockfile.acquire("collector")
    assert raised.value.status.host == "some-other-host"
    assert paths.lock_file("collector").exists()


def test_release_removes_our_lock():
    lock = lockfile.acquire("collector")
    lockfile.release(lock)
    assert not paths.lock_file("collector").exists()
    lockfile.release(lock)


def test_release_does_not_remove_another_process_lock(holder):
    ours = lockfile.Lock(
        path=paths.lock_file("collector"),
        status=lockfile.LockStatus(
            pid=os.getpid(),
            host=current_host(),
            role="collector",
            create_time=psutil.Process().create_time(),
            schema=1,
        ),
    )
    lockfile.release(ours)
    assert lockfile.status("collector").pid == holder.pid


def test_release_after_a_reclaim_leaves_the_new_owner():
    lock = lockfile.acquire("collector")
    _write_lock(pid=_dead_pid())
    lockfile.release(lock)
    assert paths.lock_file("collector").exists()


def test_status_is_read_only():
    assert lockfile.status("collector") is None
    _write_lock(pid=_dead_pid())
    before = paths.lock_file("collector").read_bytes()
    assert lockfile.status("collector") is None
    assert paths.lock_file("collector").read_bytes() == before


def test_status_reports_the_live_owner():
    lock = lockfile.acquire("collector")
    assert lockfile.status("collector") == lock.status


def test_refusal_message_is_in_the_pack():
    assert phrases.pick("instance.already_running") != ""
