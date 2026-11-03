"""The Windows half of the fix for issue #10: the hook that runs before anything else when `python -m timeexisting collect` starts, with every call to Windows and to `Popen` injected, and two tests against the real launcher. The integration test starts a collector through the venv's own `pythonw.exe` into a temporary state directory and reads its log; the contract test pins what the base interpreter does with `__PYVENV_LAUNCHER__`, which the respawn depends on."""

import contextlib
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

if sys.platform != "win32":
    pytest.skip("the respawn out of a launcher's job is Windows-only", allow_module_level=True)

from timeexisting import logging_setup, paths
from timeexisting.collector import daemon, lockfile, respawn, respawn_win32, spawn
from timeexisting.collector.respawn import GUARD_VARIABLE, LAUNCHER_VARIABLE, Decision

_VENV = r"C:\venv\Scripts\pythonw.exe"
_BASE = r"C:\base\pythonw.exe"
_LAUNCHER = Path(sys.executable).with_name("pythonw.exe")
_BASE_EXECUTABLE = getattr(sys, "_base_executable", sys.executable)

under_a_launcher = pytest.mark.skipif(
    not _LAUNCHER.is_file() or Path(_BASE_EXECUTABLE).resolve() == Path(sys.executable).resolve(),
    reason="this interpreter does not run under a venv launcher",
)


def _record_popen():
    calls: list[tuple[list[str], dict]] = []

    def popen(argv, **options):
        calls.append((argv, options))

    return calls, popen


def _hook(argv=("collect",), **overrides):
    calls, popen = _record_popen()
    seams = {
        "in_job": lambda: True,
        "limit_flags": lambda: 0x3000,
        "console": lambda: False,
        "environ": {"PATH": r"C:\bin"},
        "popen": popen,
        "executable": _VENV,
        "base_executable": _BASE,
    }
    seams.update(overrides)
    return calls, respawn_win32.hook(list(argv), **seams)


# The hook with every seam injected.


def test_the_hook_respawns_and_exits_before_any_lock(caplog):
    caplog.set_level(logging.INFO, logger="timeexisting")
    calls, popen = _record_popen()
    with pytest.raises(SystemExit) as exited:
        respawn_win32.hook(
            ["collect"],
            in_job=lambda: True,
            limit_flags=lambda: 0x3000,
            console=lambda: False,
            environ={"PATH": r"C:\bin"},
            popen=popen,
            executable=_VENV,
            base_executable=_BASE,
        )
    assert exited.value.code == 0
    [(argv, options)] = calls
    assert argv == [_BASE, "-m", "timeexisting", "collect"]
    assert options["env"] == {"PATH": r"C:\bin", LAUNCHER_VARIABLE: _VENV, GUARD_VARIABLE: "1"}
    assert options["creationflags"] == spawn._WINDOWS_FLAGS
    assert options["close_fds"] is True
    for stream in ("stdin", "stdout", "stderr"):
        assert options[stream] is subprocess.DEVNULL
    assert not paths.lock_file(daemon.ROLE).exists()
    assert logging_setup._handler is None, "the first process must release the log file before it exits"
    assert (
        f"respawn check: pid {os.getpid()}, executable {_VENV}, in job yes, limit flags 0x3000 ("
        in caplog.text
    )
    assert "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in caplog.text
    assert f"respawning outside the job: {_BASE}" in caplog.text


def test_breakaway_ok_only_adds_create_breakaway_from_job():
    calls, popen = _record_popen()
    with pytest.raises(SystemExit):
        respawn_win32.hook(
            ["collect"],
            in_job=lambda: True,
            limit_flags=lambda: 0x2800,
            console=lambda: False,
            environ={},
            popen=popen,
            executable=_VENV,
            base_executable=_BASE,
        )
    [(_argv, options)] = calls
    assert options["creationflags"] == spawn._WINDOWS_FLAGS | respawn.CREATE_BREAKAWAY_FROM_JOB


def test_a_console_run_does_not_respawn(caplog):
    caplog.set_level(logging.INFO, logger="timeexisting")
    calls, decision = _hook(console=lambda: True)
    assert calls == []
    assert isinstance(decision, Decision)
    assert not decision.respawn
    assert "console run, respawn skipped" in caplog.text


def test_the_guard_in_the_environment_stays(caplog):
    caplog.set_level(logging.INFO, logger="timeexisting")
    calls, decision = _hook(environ={GUARD_VARIABLE: "1"})
    assert calls == []
    assert not decision.respawn
    assert GUARD_VARIABLE in caplog.text


def test_a_job_without_breakaway_stays_and_says_so(caplog):
    caplog.set_level(logging.INFO, logger="timeexisting")
    calls, decision = _hook(limit_flags=lambda: 0x2000)
    assert calls == []
    assert not decision.respawn
    assert "without breakaway" in caplog.text


def test_a_failing_seam_is_logged_and_the_collector_carries_on(caplog):
    caplog.set_level(logging.INFO, logger="timeexisting")

    def explode():
        raise RuntimeError("boom")

    calls, decision = _hook(in_job=explode)
    assert calls == []
    assert not decision.respawn
    assert "in job unavailable: boom" in caplog.text


def test_a_popen_that_fails_is_logged_and_the_collector_carries_on(caplog):
    caplog.set_level(logging.INFO, logger="timeexisting")

    def refuse(*_argv, **_options):
        raise OSError(5, "Access is denied")

    decision = respawn_win32.hook(
        ["collect"],
        in_job=lambda: True,
        limit_flags=lambda: 0x3000,
        console=lambda: False,
        environ={},
        popen=refuse,
        executable=_VENV,
        base_executable=_BASE,
    )
    assert not decision.respawn
    assert "respawn failed" in caplog.text
    assert "Access is denied" in caplog.text


def test_other_argv_reads_nothing():
    def explode():
        raise AssertionError("a seam was read")

    for argv in ([], ["collect", "status"], ["--no-collector"]):
        calls, decision = _hook(argv, in_job=explode, limit_flags=explode, console=explode)
        assert calls == []
        assert decision is None


def test_console_attached_answers_a_bool():
    assert isinstance(respawn_win32.console_attached(), bool)


# Against the real launcher.


@under_a_launcher
def test_a_collector_spawned_through_the_venv_pythonw_outlives_its_launchers_job(monkeypatch):
    """The collector is started exactly as the viewer starts it (`spawn.command` and `spawn._WINDOWS_FLAGS`), into this test's isolated state directory, which the child inherits through the environment. The launcher must return 0 while the lock stays live, so the collector has left the launcher's job; the log must hold the first process's line with the kill-on-close flag and a later probe line from the pid that holds the lock."""
    monkeypatch.delenv(GUARD_VARIABLE, raising=False)
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text('[collector]\ntick = "2s"\npoll = "2s"\n', encoding="utf-8")
    started = time.monotonic()
    launcher = subprocess.Popen(
        spawn.command(),
        creationflags=spawn._WINDOWS_FLAGS,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        env=dict(os.environ),
    )
    owner = None
    try:
        while time.monotonic() - started < 30:
            owner = lockfile.status(daemon.ROLE)
            if owner is not None:
                break
            time.sleep(0.1)
        locked_after = time.monotonic() - started
        assert owner is not None, "the collector never took its lock"
        print(f"launch to lock: {locked_after:.2f}s, lock pid {owner.pid}, launcher pid {launcher.pid}")
        try:
            code = launcher.wait(10)
        except subprocess.TimeoutExpired:
            pytest.fail("the launcher is still waiting on its child: the collector is inside its job")
        assert code == 0
        time.sleep(3)
        still = lockfile.status(daemon.ROLE)
        assert still is not None and still.pid == owner.pid, "the collector died with the launcher's job"
        lines = paths.log_file(daemon.ROLE).read_text(encoding="utf-8").splitlines()
        earlier = [line for line in lines if "KILL_ON_JOB_CLOSE" in line]
        assert earlier, "no line records the launcher's kill-on-close job"
        assert f"pid {owner.pid}," not in earlier[0], (
            "the first line must come from the process the launcher ran"
        )
        probes = [line for line in lines if "job probe: " in line]
        assert probes, "the final collector wrote no probe line"
        assert f"job probe: pid {owner.pid}," in probes[-1]
        assert probes[-1] != earlier[0]
        print("earlier: " + earlier[0])
        print("final:   " + probes[-1])
    finally:
        paths.stop_file().touch()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and lockfile.status(daemon.ROLE) is not None:
            time.sleep(0.25)
        if owner is not None and psutil.pid_exists(owner.pid):
            with contextlib.suppress(psutil.Error):
                psutil.Process(owner.pid).kill()
        if launcher.poll() is None:
            launcher.kill()


@under_a_launcher
def test_the_base_interpreter_with_the_launcher_variable_reports_the_venv():
    """The contract the respawn relies on, pinned: given `__PYVENV_LAUNCHER__`, the base interpreter reports the venv's executable and prefix, and clears the variable from its environment."""
    env = dict(os.environ)
    env[LAUNCHER_VARIABLE] = sys.executable
    code = "import os, sys; print(sys.executable); print(sys.prefix); print(os.environ.get('__PYVENV_LAUNCHER__'))"
    result = subprocess.run(
        [_BASE_EXECUTABLE, "-c", code], env=env, capture_output=True, text=True, timeout=60, check=True
    )
    assert result.stdout.splitlines() == [sys.executable, sys.prefix, "None"]
