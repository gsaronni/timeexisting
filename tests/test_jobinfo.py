"""The pure half of the job object probe (issue #10 evidence): the limit flag decoder, the probe result gathered from injectable calls, and the one log line. Runs on every platform; nothing here touches Windows."""

import pytest

from timeexisting.collector import jobinfo
from timeexisting.collector.jobinfo import JobProbe, decode_limit_flags, format_probe, gather

_DIE = "JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION"
_BREAKAWAY = "JOB_OBJECT_LIMIT_BREAKAWAY_OK"
_SILENT = "JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK"
_KILL = "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE"


# The decoder.


def test_zero_sets_no_flag():
    assert decode_limit_flags(0) == ()


@pytest.mark.parametrize(
    ("flags", "name"),
    [(0x400, _DIE), (0x800, _BREAKAWAY), (0x1000, _SILENT), (0x2000, _KILL)],
)
def test_each_single_flag_decodes_to_its_name(flags, name):
    assert decode_limit_flags(flags) == (name,)


def test_the_launchers_combination_decodes_in_bit_order():
    assert decode_limit_flags(0x3800) == (_BREAKAWAY, _SILENT, _KILL)


def test_an_unknown_bit_is_shown_as_hex_not_dropped():
    assert decode_limit_flags(0x10000) == ("0x10000",)
    assert decode_limit_flags(0x2000 | 0x10000) == (_KILL, "0x10000")


def test_every_known_bit_round_trips_in_bit_order():
    everything = sum(jobinfo.LIMIT_FLAG_NAMES)
    assert decode_limit_flags(everything) == tuple(
        jobinfo.LIMIT_FLAG_NAMES[bit] for bit in sorted(jobinfo.LIMIT_FLAG_NAMES)
    )


def test_the_four_launcher_relevant_bits_are_named():
    assert {0x400: _DIE, 0x800: _BREAKAWAY, 0x1000: _SILENT, 0x2000: _KILL}.items() <= (
        jobinfo.LIMIT_FLAG_NAMES.items()
    )


def test_a_negative_value_is_refused():
    with pytest.raises(ValueError):
        decode_limit_flags(-1)


# The formatter.


def test_a_process_in_a_kill_on_close_job_formats_to_one_line():
    probe = JobProbe(
        pid=4321,
        executable=r"C:\venv\Scripts\pythonw.exe",
        image=r"C:\Python314\pythonw.exe",
        parent_pid=1200,
        parent_image=r"C:\venv\Scripts\pythonw.exe",
        in_job=True,
        limit_flags=0x3800,
    )
    assert format_probe(probe) == (
        r"job probe: pid 4321, executable C:\venv\Scripts\pythonw.exe, image C:\Python314\pythonw.exe, "
        r"parent pid 1200, parent image C:\venv\Scripts\pythonw.exe, in job yes, "
        f"limit flags 0x3800 ({_BREAKAWAY}, {_SILENT}, {_KILL})"
    )


def test_a_process_outside_any_job_formats_without_flags():
    probe = JobProbe(
        pid=1,
        executable="python",
        image="python.exe",
        parent_pid=2,
        parent_image="explorer.exe",
        in_job=False,
        limit_flags=None,
    )
    line = format_probe(probe)
    assert line == (
        "job probe: pid 1, executable python, image python.exe, parent pid 2, parent image explorer.exe, in job no"
    )
    assert "limit flags" not in line


def test_a_job_with_no_limit_flags_says_none():
    probe = JobProbe(
        pid=1, executable="python", image="i", parent_pid=2, parent_image="p", in_job=True, limit_flags=0
    )
    assert format_probe(probe).endswith("in job yes, limit flags 0x0 (none)")


def test_unavailable_fields_are_printed_as_their_reason():
    probe = JobProbe(
        pid=1,
        executable="python",
        image="unavailable: OpenProcess failed, 87 The parameter is incorrect.",
        parent_pid="unavailable: no parent",
        parent_image="unavailable: no parent pid",
        in_job="unavailable: IsProcessInJob failed, 5 Access is denied.",
        limit_flags="unavailable: QueryInformationJobObject failed, 87 The parameter is incorrect.",
    )
    assert format_probe(probe) == (
        "job probe: pid 1, executable python, "
        "image unavailable: OpenProcess failed, 87 The parameter is incorrect., "
        "parent pid unavailable: no parent, "
        "parent image unavailable: no parent pid, "
        "in job unavailable: IsProcessInJob failed, 5 Access is denied., "
        "limit flags unavailable: QueryInformationJobObject failed, 87 The parameter is incorrect."
    )


def test_a_single_line_whatever_the_values():
    probe = JobProbe(
        pid=1,
        executable="a\nb",
        image="e\rf",
        parent_pid=2,
        parent_image="c\r\nd",
        in_job=True,
        limit_flags=0x2000,
    )
    assert "\n" not in format_probe(probe)
    assert "\r" not in format_probe(probe)


# Gathering from the underlying calls.


class _Raising:
    def __init__(self, message: str = "boom") -> None:
        self.message = message
        self.calls = 0

    def __call__(self, *_args):
        self.calls += 1
        raise OSError(self.message)


class _Images:
    """An `image_path` that records the pid of every call."""

    def __init__(self) -> None:
        self.pids: list[int] = []

    def __call__(self, pid: int) -> str:
        self.pids.append(pid)
        return f"image {pid}"


def test_a_probe_whose_calls_all_raise_still_returns_a_result():
    parent_pid, image_path, in_job, limit_flags = (
        _Raising("no parent"),
        _Raising(),
        _Raising("denied"),
        _Raising(),
    )
    result = gather(
        77, "python", parent_pid=parent_pid, image_path=image_path, in_job=in_job, limit_flags=limit_flags
    )
    assert result == JobProbe(
        pid=77,
        executable="python",
        image="unavailable: boom",
        parent_pid="unavailable: no parent",
        parent_image="unavailable: no parent pid",
        in_job="unavailable: denied",
        limit_flags="unavailable: boom",
    )
    # The image path is tried once, for our own pid; the parent's needs the parent pid, which is unavailable.
    assert image_path.calls == 1
    # Whether the process is in a job is unknown, so the flags are still tried.
    assert limit_flags.calls == 1


def test_an_error_without_a_message_names_its_type():
    result = gather(
        1,
        "python",
        parent_pid=_Raising(""),
        image_path=lambda _pid: "p",
        in_job=lambda: False,
        limit_flags=lambda: 0,
    )
    assert result.parent_pid == "unavailable: OSError"


def test_a_process_outside_any_job_never_asks_for_flags():
    limit_flags = _Raising()
    result = gather(
        1,
        "python",
        parent_pid=lambda: 9,
        image_path=lambda pid: f"image of {pid}",
        in_job=lambda: False,
        limit_flags=limit_flags,
    )
    assert result == JobProbe(
        pid=1,
        executable="python",
        image="image of 1",
        parent_pid=9,
        parent_image="image of 9",
        in_job=False,
        limit_flags=None,
    )
    assert limit_flags.calls == 0


def test_a_process_in_a_job_passes_every_value_through():
    result = gather(
        5,
        r"C:\p\pythonw.exe",
        parent_pid=lambda: 3,
        image_path=_Images(),
        in_job=lambda: True,
        limit_flags=lambda: 0x3800,
    )
    assert result == JobProbe(
        pid=5,
        executable=r"C:\p\pythonw.exe",
        image="image 5",
        parent_pid=3,
        parent_image="image 3",
        in_job=True,
        limit_flags=0x3800,
    )


def test_the_own_image_is_read_for_the_probes_pid_and_the_parents_for_the_parent():
    images = _Images()
    gather(5, "python", parent_pid=lambda: 3, image_path=images, in_job=lambda: False, limit_flags=lambda: 0)
    assert images.pids == [5, 3]


def test_only_the_failing_call_is_unavailable():
    result = gather(
        5,
        "python",
        parent_pid=lambda: 3,
        image_path=_Raising("OpenProcess failed, 5 Access is denied."),
        in_job=lambda: True,
        limit_flags=lambda: 0,
    )
    assert result.parent_pid == 3
    assert result.image == "unavailable: OpenProcess failed, 5 Access is denied."
    assert result.parent_image == "unavailable: OpenProcess failed, 5 Access is denied."
    assert result.in_job is True
    assert result.limit_flags == 0


def test_the_result_is_frozen():
    result = gather(
        1,
        "python",
        parent_pid=lambda: 2,
        image_path=lambda _pid: "p",
        in_job=lambda: False,
        limit_flags=lambda: 0,
    )
    with pytest.raises(AttributeError):
        result.pid = 2  # type: ignore[misc]
