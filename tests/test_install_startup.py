"""The Startup shortcut script, run for real under PowerShell 7 against a temporary Startup folder and a fake `uv` whose `uv tool dir` names a temporary tool directory. The user's own Startup folder and real uv tools are never touched: every run passes `-StartupFolder` and a PATH holding only the fake `uv` and System32."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

if sys.platform != "win32":
    pytest.skip("the Startup shortcut is Windows only", allow_module_level=True)

PWSH = shutil.which("pwsh")
if PWSH is None:
    pytest.skip("PowerShell 7 (pwsh) is not installed", allow_module_level=True)

win32com_client = pytest.importorskip("win32com.client")

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "install-startup.ps1"
_SHORTCUT = "timeexisting collector.lnk"
_SYSTEM32 = str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32")


@pytest.fixture
def tool_dir(tmp_path):
    tools = tmp_path / "uv-tools"
    (tools / "timeexisting" / "Scripts").mkdir(parents=True)
    (tools / "timeexisting" / "Scripts" / "pythonw.exe").touch()
    return tools


@pytest.fixture
def fake_uv(tmp_path, tool_dir):
    """A directory holding `uv.cmd`, which answers `uv tool dir` with the temporary tool directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv.cmd").write_text(f'@echo off\r\nif "%1 %2"=="tool dir" echo {tool_dir}\r\n')
    return bin_dir


@pytest.fixture
def startup(tmp_path):
    folder = tmp_path / "Startup"
    folder.mkdir()
    return folder


@pytest.fixture
def profile(tmp_path):
    folder = tmp_path / "profile"
    folder.mkdir()
    return folder


def _run(startup: Path, profile: Path, path: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    command = [
        PWSH,
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(_SCRIPT),
        "-StartupFolder",
        str(startup),
        *arguments,
    ]
    env = {**os.environ, "PATH": path, "USERPROFILE": str(profile)}
    return subprocess.run(
        command, capture_output=True, text=True, timeout=60, cwd=startup.parent, env=env, check=False
    )


@pytest.fixture
def run(startup, profile, fake_uv):
    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return _run(startup, profile, os.pathsep.join([str(fake_uv), _SYSTEM32]), *arguments)

    return run


def _read_shortcut(path: Path):
    return win32com_client.Dispatch("WScript.Shell").CreateShortcut(str(path))


def test_install_creates_the_shortcut_to_the_tool_pythonw(run, startup, tool_dir, profile):
    result = run()

    assert result.returncode == 0, result.stderr
    link = _read_shortcut(startup / _SHORTCUT)
    assert Path(link.TargetPath) == tool_dir / "timeexisting" / "Scripts" / "pythonw.exe"
    assert link.Arguments == "-m timeexisting collect"
    assert Path(link.WorkingDirectory) == profile


def test_install_twice_replaces_the_shortcut(run, startup):
    assert run().returncode == 0
    result = run()
    assert result.returncode == 0, result.stderr
    assert [path.name for path in startup.iterdir()] == [_SHORTCUT]


def test_install_replaces_an_existing_dev_shortcut(run, startup, tool_dir):
    """A shortcut of the same name from the old script, pointing at a repository venv, is replaced, not kept beside the new one."""
    dev_target = startup.parent / "repo" / ".venv" / "Scripts" / "pythonw.exe"
    dev_target.parent.mkdir(parents=True)
    dev_target.touch()
    old = _read_shortcut(startup / _SHORTCUT)
    old.TargetPath = str(dev_target)
    old.Save()

    result = run()

    assert result.returncode == 0, result.stderr
    assert [path.name for path in startup.iterdir()] == [_SHORTCUT]
    link = _read_shortcut(startup / _SHORTCUT)
    assert Path(link.TargetPath) == tool_dir / "timeexisting" / "Scripts" / "pythonw.exe"


def test_what_if_creates_nothing(run, startup):
    result = run("-WhatIf")
    assert result.returncode == 0, result.stderr
    assert "What if" in result.stdout
    assert not (startup / _SHORTCUT).exists()


def test_uninstall_removes_the_shortcut(run, startup):
    assert run().returncode == 0
    result = run("-Uninstall")
    assert result.returncode == 0, result.stderr
    assert not (startup / _SHORTCUT).exists()


def test_uninstall_with_what_if_keeps_the_shortcut(run, startup):
    assert run().returncode == 0
    result = run("-Uninstall", "-WhatIf")
    assert result.returncode == 0, result.stderr
    assert (startup / _SHORTCUT).exists()


def test_uninstall_without_a_shortcut_is_not_an_error(run):
    result = run("-Uninstall")
    assert result.returncode == 0, result.stderr
    assert "nothing to remove" in result.stdout


def test_refuses_without_uv(startup, profile):
    result = _run(startup, profile, _SYSTEM32)
    assert result.returncode == 1
    assert "uv is not on PATH" in result.stderr
    assert not (startup / _SHORTCUT).exists()


def test_refuses_without_the_tool_pythonw(run, startup, tool_dir):
    (tool_dir / "timeexisting" / "Scripts" / "pythonw.exe").unlink()
    result = run()
    assert result.returncode == 1
    assert "no pythonw.exe" in result.stderr
    assert "uv tool install" in result.stderr
    assert not (startup / _SHORTCUT).exists()
