"""The dev-state script, run for real under PowerShell 7 against a copy of the script in a fake repository and a throwaway venv. The repository's own .venv is never touched: every run passes `-Venv`."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from timeexisting import paths

if sys.platform != "win32":
    pytest.skip("the dev-state script is tested on Windows", allow_module_level=True)

PWSH = shutil.which("pwsh")
if PWSH is None:
    pytest.skip("PowerShell 7 (pwsh) is not installed", allow_module_level=True)

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dev-setup.ps1"
_PTH = "timeexisting_devstate.pth"
_SHOW = "import os; print(os.environ.get('TIMEEXISTING_STATE_DIR')); print(os.environ.get('TIMEEXISTING_CONFIG_DIR'))"


@pytest.fixture(scope="module")
def venv(tmp_path_factory):
    """One throwaway venv for the module, built from the interpreter running the tests, without pip."""
    location = tmp_path_factory.mktemp("throwaway") / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(location)], check=True, timeout=120)
    return location


@pytest.fixture
def site_packages(venv):
    location = Path(
        subprocess.run(
            [
                str(venv / "Scripts" / "python.exe"),
                "-I",
                "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    (location / _PTH).unlink(missing_ok=True)
    yield location
    (location / _PTH).unlink(missing_ok=True)


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(_SCRIPT, repo / "scripts" / _SCRIPT.name)
    return repo


def _run(repo: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    command = [
        PWSH,
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(repo / "scripts" / _SCRIPT.name),
        *arguments,
    ]
    # Run from elsewhere, so the repository can only come from the script's own location.
    return subprocess.run(command, capture_output=True, text=True, timeout=60, cwd=repo.parent, check=False)


def _environment_seen_by(venv: Path, **overrides: str) -> list[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in (paths.STATE_DIR_ENV, paths.CONFIG_DIR_ENV)
    }
    env.update(overrides)
    result = subprocess.run(
        [str(venv / "Scripts" / "python.exe"), "-c", _SHOW],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return result.stdout.splitlines()


def test_writes_one_pth_that_points_the_venv_at_devstate(repository, venv, site_packages):
    result = _run(repository, "-Venv", str(venv))

    assert result.returncode == 0, result.stderr
    assert [path.name for path in site_packages.glob("timeexisting*.pth")] == [_PTH]
    assert _environment_seen_by(venv) == [
        str(repository / ".devstate" / "state"),
        str(repository / ".devstate" / "config"),
    ]


def test_running_twice_leaves_one_unchanged_file(repository, venv, site_packages):
    assert _run(repository, "-Venv", str(venv)).returncode == 0
    first = (site_packages / _PTH).read_bytes()

    result = _run(repository, "-Venv", str(venv))

    assert result.returncode == 0, result.stderr
    assert "Unchanged" in result.stdout
    assert (site_packages / _PTH).read_bytes() == first
    assert [path.name for path in site_packages.glob("timeexisting*.pth")] == [_PTH]


def test_rewrites_a_stale_file(repository, venv, site_packages):
    (site_packages / _PTH).write_text("import os\n")

    result = _run(repository, "-Venv", str(venv))

    assert result.returncode == 0, result.stderr
    assert "Wrote" in result.stdout
    assert _environment_seen_by(venv)[0] == str(repository / ".devstate" / "state")


@pytest.mark.usefixtures("site_packages")
def test_an_explicit_value_still_wins(repository, venv, tmp_path):
    assert _run(repository, "-Venv", str(venv)).returncode == 0
    explicit = str(tmp_path / "explicit")

    seen = _environment_seen_by(venv, **{paths.STATE_DIR_ENV: explicit})

    assert seen == [explicit, str(repository / ".devstate" / "config")]


def test_what_if_writes_nothing(repository, venv, site_packages):
    result = _run(repository, "-Venv", str(venv), "-WhatIf")
    assert result.returncode == 0, result.stderr
    assert "What if" in result.stdout
    assert not (site_packages / _PTH).exists()


def test_uninstall_removes_the_pth(repository, venv, site_packages):
    assert _run(repository, "-Venv", str(venv)).returncode == 0
    result = _run(repository, "-Venv", str(venv), "-Uninstall")
    assert result.returncode == 0, result.stderr
    assert not (site_packages / _PTH).exists()
    assert _environment_seen_by(venv) == ["None", "None"]


def test_refuses_without_a_venv(repository, tmp_path):
    result = _run(repository, "-Venv", str(tmp_path / "missing"))
    assert result.returncode == 1
    assert "no venv" in result.stderr


def test_refuses_without_an_interpreter(repository, tmp_path):
    empty = tmp_path / "empty-venv"
    empty.mkdir()
    result = _run(repository, "-Venv", str(empty))
    assert result.returncode == 1
    assert "no interpreter" in result.stderr
