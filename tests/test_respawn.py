"""The pure half of the fix for issue #10: whether a collector that finds itself inside a launcher's kill-on-close job respawns outside it, the command and environment of that respawn, and the line that records what it found. Runs on every platform; nothing here touches Windows or a process."""

import ast
from pathlib import Path

import pytest

from timeexisting.collector import daemon, respawn, spawn
from timeexisting.collector.respawn import (
    CREATE_BREAKAWAY_FROM_JOB,
    GUARD_VARIABLE,
    LAUNCHER_VARIABLE,
    Decision,
    command,
    decide,
    environment,
    format_check,
    wants_hook,
)

_KILL = 0x2000
_SILENT = 0x1000
_BREAKAWAY = 0x800
_VENV = r"C:\venv\Scripts\pythonw.exe"
_BASE = r"C:\base\pythonw.exe"
_SRC = Path(__file__).resolve().parent.parent / "src" / "timeexisting"


def _decide(**overrides):
    arguments = {
        "in_job": True,
        "limit_flags": _KILL | _SILENT,
        "console": False,
        "guarded": False,
        "executable": _VENV,
        "base_executable": _BASE,
    }
    arguments.update(overrides)
    return decide(**arguments)


# The decision.


def test_kill_on_close_with_silent_breakaway_respawns_with_spawns_flags():
    decision = _decide()
    assert decision.respawn
    assert decision.creation_flags == spawn._WINDOWS_FLAGS
    assert "silent breakaway" in decision.reason


def test_kill_on_close_with_breakaway_ok_only_respawns_with_create_breakaway_from_job():
    decision = _decide(limit_flags=_KILL | _BREAKAWAY)
    assert decision.respawn
    assert decision.creation_flags == spawn._WINDOWS_FLAGS | CREATE_BREAKAWAY_FROM_JOB
    assert CREATE_BREAKAWAY_FROM_JOB == 0x01000000


def test_kill_on_close_with_both_breakaway_bits_needs_no_breakaway_flag():
    decision = _decide(limit_flags=_KILL | _SILENT | _BREAKAWAY)
    assert decision.respawn
    assert decision.creation_flags == spawn._WINDOWS_FLAGS


def test_kill_on_close_without_either_breakaway_stays():
    decision = _decide(limit_flags=_KILL)
    assert not decision.respawn
    assert "without breakaway" in decision.reason


def test_a_job_without_kill_on_close_stays():
    decision = _decide(limit_flags=_SILENT)
    assert not decision.respawn
    assert "without KILL_ON_JOB_CLOSE" in decision.reason


def test_outside_any_job_stays():
    decision = _decide(in_job=False, limit_flags=None)
    assert not decision.respawn
    assert decision.reason == "not in a job"


@pytest.mark.parametrize(
    ("in_job", "limit_flags"),
    [("unavailable: IsProcessInJob failed", None), (True, "unavailable: QueryInformationJobObject failed")],
)
def test_an_unavailable_field_stays(in_job, limit_flags):
    decision = _decide(in_job=in_job, limit_flags=limit_flags)
    assert not decision.respawn
    assert "unavailable" in decision.reason


def test_the_guard_prevents_a_second_respawn():
    decision = _decide(guarded=True)
    assert not decision.respawn
    assert GUARD_VARIABLE in decision.reason


def test_a_console_run_does_not_respawn():
    decision = _decide(console=True)
    assert not decision.respawn
    assert decision.reason == "console run, respawn skipped"


@pytest.mark.parametrize("base_executable", [None, _VENV])
def test_no_base_interpreter_distinct_from_the_executable_stays(base_executable):
    decision = _decide(base_executable=base_executable)
    assert not decision.respawn
    assert "base interpreter" in decision.reason


def test_the_decision_is_frozen():
    with pytest.raises(AttributeError):
        _decide().respawn = False  # type: ignore[misc]


def test_a_stay_carries_no_creation_flags():
    assert _decide(console=True).creation_flags == 0
    assert Decision(respawn=False, reason="x").creation_flags == 0


# The command, the environment and the gate.


def test_the_command_runs_the_base_interpreter_as_the_collector():
    assert command(_BASE) == [_BASE, "-m", "timeexisting", "collect"]


def test_the_environment_adds_the_launcher_and_the_guard_and_changes_nothing_else():
    original = {"TIMEEXISTING_STATE_DIR": r"C:\state", "PATH": r"C:\bin"}
    env = environment(original, _VENV)
    assert env == {
        "TIMEEXISTING_STATE_DIR": r"C:\state",
        "PATH": r"C:\bin",
        LAUNCHER_VARIABLE: _VENV,
        GUARD_VARIABLE: "1",
    }
    assert original == {"TIMEEXISTING_STATE_DIR": r"C:\state", "PATH": r"C:\bin"}
    assert LAUNCHER_VARIABLE == "__PYVENV_LAUNCHER__"


@pytest.mark.parametrize(
    ("platform", "argv", "wanted"),
    [
        ("win32", ["collect"], True),
        ("linux", ["collect"], False),
        ("darwin", ["collect"], False),
        ("win32", [], False),
        ("win32", ["collect", "status"], False),
        ("win32", ["collect", "stop"], False),
        ("win32", ["--no-collector"], False),
    ],
)
def test_the_hook_is_wanted_only_on_windows_for_a_bare_collect(platform, argv, wanted):
    assert wants_hook(platform, argv) is wanted


def test_the_hook_role_is_the_daemons():
    assert respawn.ROLE == daemon.ROLE


def test_spawn_shares_the_detached_creation_flags():
    assert spawn._WINDOWS_FLAGS == respawn.DETACHED_FLAGS == 0x00000008 | 0x00000200 | 0x08000000


# The check line.


def test_the_check_line_names_the_flags():
    assert format_check(7, _VENV, True, _KILL | _SILENT) == (
        f"respawn check: pid 7, executable {_VENV}, in job yes, "
        "limit flags 0x3000 (JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)"
    )


def test_the_check_line_outside_a_job_has_no_flags_part():
    assert format_check(7, _VENV, False, None) == f"respawn check: pid 7, executable {_VENV}, in job no"


def test_the_check_line_passes_unavailable_fields_through():
    assert format_check(7, _VENV, "unavailable: IsProcessInJob failed", "unavailable: no job") == (
        f"respawn check: pid 7, executable {_VENV}, in job unavailable: IsProcessInJob failed, "
        "limit flags unavailable: no job"
    )


# The import budget of the hook: it runs before anything else, so it must stay cheap.


def _module_level_imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_the_hook_module_imports_only_the_ctypes_seams_and_logging_setup():
    imports = _module_level_imports(_SRC / "collector" / "respawn_win32.py")
    project = {name for name in imports if name.startswith("timeexisting")}
    assert project <= {
        "timeexisting",
        "timeexisting.logging_setup",
        "timeexisting.collector",
        "timeexisting.collector.jobinfo",
        "timeexisting.collector.jobinfo_win32",
        "timeexisting.collector.respawn",
    }
    assert "psutil" not in imports
    assert not any(name.startswith("win32") or name == "pywintypes" for name in imports)


def test_the_ctypes_seams_module_imports_psutil_lazily():
    assert "psutil" not in _module_level_imports(_SRC / "collector" / "jobinfo_win32.py")


def test_main_runs_the_hook_before_importing_the_cli():
    tree = ast.parse((_SRC / "__main__.py").read_text(encoding="utf-8"))
    imports = sorted(
        (node.lineno, node.module) for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    names = [module for _line, module in imports]
    assert "timeexisting.collector.respawn" in names
    assert "timeexisting.collector.respawn_win32" in names
    assert names.index("timeexisting.collector.respawn_win32") < names.index("timeexisting.cli")
