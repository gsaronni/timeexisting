"""The Startup shortcut script, run for real under PowerShell 7 against a copy of the script in a fake repository and a temporary Startup folder. The user's own Startup folder is never touched: every run passes `-StartupFolder`."""

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


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(_SCRIPT, repo / "scripts" / _SCRIPT.name)
    (repo / ".venv" / "Scripts").mkdir(parents=True)
    (repo / ".venv" / "Scripts" / "pythonw.exe").touch()
    return repo


@pytest.fixture
def startup(tmp_path):
    folder = tmp_path / "Startup"
    folder.mkdir()
    return folder


def _run(repo: Path, startup: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    script = repo / "scripts" / _SCRIPT.name
    command = [
        PWSH,
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(script),
        "-StartupFolder",
        str(startup),
        *arguments,
    ]
    # Run from elsewhere, so the repository can only come from the script's own location.
    return subprocess.run(
        command, capture_output=True, text=True, timeout=60, cwd=startup.parent, check=False
    )


def _read_shortcut(path: Path):
    return win32com_client.Dispatch("WScript.Shell").CreateShortcut(str(path))


def test_install_creates_the_shortcut_to_the_venv_pythonw(repository, startup):
    result = _run(repository, startup)

    assert result.returncode == 0, result.stderr
    link = _read_shortcut(startup / _SHORTCUT)
    assert Path(link.TargetPath) == repository / ".venv" / "Scripts" / "pythonw.exe"
    assert link.Arguments == "-m timeexisting collect"
    assert Path(link.WorkingDirectory) == repository


def test_install_twice_replaces_the_shortcut(repository, startup):
    assert _run(repository, startup).returncode == 0
    result = _run(repository, startup)
    assert result.returncode == 0, result.stderr
    assert [path.name for path in startup.iterdir()] == [_SHORTCUT]


def test_what_if_creates_nothing(repository, startup):
    result = _run(repository, startup, "-WhatIf")
    assert result.returncode == 0, result.stderr
    assert "What if" in result.stdout
    assert not (startup / _SHORTCUT).exists()


def test_uninstall_removes_the_shortcut(repository, startup):
    assert _run(repository, startup).returncode == 0
    result = _run(repository, startup, "-Uninstall")
    assert result.returncode == 0, result.stderr
    assert not (startup / _SHORTCUT).exists()


def test_uninstall_with_what_if_keeps_the_shortcut(repository, startup):
    assert _run(repository, startup).returncode == 0
    result = _run(repository, startup, "-Uninstall", "-WhatIf")
    assert result.returncode == 0, result.stderr
    assert (startup / _SHORTCUT).exists()


def test_uninstall_without_a_shortcut_is_not_an_error(repository, startup):
    result = _run(repository, startup, "-Uninstall")
    assert result.returncode == 0, result.stderr
    assert "nothing to remove" in result.stdout


def test_refuses_without_a_venv(repository, startup):
    shutil.rmtree(repository / ".venv")
    result = _run(repository, startup)
    assert result.returncode == 1
    assert "no venv" in result.stderr
    assert not (startup / _SHORTCUT).exists()


def test_refuses_without_pythonw(repository, startup):
    (repository / ".venv" / "Scripts" / "pythonw.exe").unlink()
    result = _run(repository, startup)
    assert result.returncode == 1
    assert "no pythonw.exe" in result.stderr
    assert not (startup / _SHORTCUT).exists()
