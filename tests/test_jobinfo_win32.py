"""The Windows I/O half of the job object probe (issue #10 evidence), against this very process. Every call here is read-only and unelevated: the process asks about itself and its parent, and opens nothing it cannot."""

import os
import sys
from pathlib import Path

import pytest

if sys.platform != "win32":
    pytest.skip("the job object probe is Windows-only", allow_module_level=True)

from timeexisting.collector import jobinfo_win32
from timeexisting.collector.jobinfo import JobProbe
from timeexisting.collector.jobinfo_win32 import JobInfoUnavailable


def test_the_real_probe_reports_this_process():
    result = jobinfo_win32.probe()
    assert isinstance(result, JobProbe)
    assert result.pid == os.getpid()
    assert result.executable == sys.executable
    assert isinstance(result.image, str)
    assert Path(result.image).name.lower() in {"python.exe", "pythonw.exe"}
    assert isinstance(result.parent_pid, int)
    assert isinstance(result.parent_image, str)
    assert isinstance(result.in_job, bool)
    if result.in_job:
        assert isinstance(result.limit_flags, int)
    else:
        assert result.limit_flags is None


def test_the_image_path_of_this_process_is_an_interpreter():
    # Under a venv launcher `sys.executable` is the launcher's path while the running image is the base interpreter, so only the file name is pinned.
    image = jobinfo_win32.image_path(os.getpid())
    assert Path(image).name.lower() in {"python.exe", "pythonw.exe"}
    assert Path(image).is_file()


def test_a_pid_that_cannot_be_opened_is_unavailable_not_an_exception_of_another_kind():
    with pytest.raises(JobInfoUnavailable, match="OpenProcess"):
        jobinfo_win32.image_path(0)


def test_this_process_answers_whether_it_is_in_a_job():
    assert isinstance(jobinfo_win32.in_job(), bool)


def test_the_limit_flags_of_this_process_are_an_int_or_unavailable():
    if jobinfo_win32.in_job():
        assert isinstance(jobinfo_win32.limit_flags(), int)
    else:
        with pytest.raises(JobInfoUnavailable, match="QueryInformationJobObject"):
            jobinfo_win32.limit_flags()


def test_the_parent_pid_is_a_live_or_recent_process_id():
    parent = jobinfo_win32.parent_pid()
    assert isinstance(parent, int)
    assert parent != os.getpid()
