"""The input probe (phase 3 step 7): the 32-bit tick arithmetic, the Windows seam's failure logging and the registry. Only the last test touches the real `GetLastInputInfo`, and only on Windows."""

import logging
import sys
from datetime import UTC, datetime, timedelta

import pytest

from timeexisting.presence import backend
from timeexisting.presence.backend import NoInput, Win32Input, input_time

_NOW = datetime(2026, 9, 17, 6, 0, 0, tzinfo=UTC)


def test_elapsed_is_the_difference_of_the_two_counts():
    assert input_time(_NOW, 1_000_000, 1_042_500) == _NOW - timedelta(seconds=42.5)


def test_the_same_count_is_input_now():
    assert input_time(_NOW, 123, 123) == _NOW


def test_a_count_that_wrapped_since_the_input_gives_the_small_elapsed():
    """About 49.7 days after boot: the input at 0xFFFFFF00, the tick count already past zero at 0x100."""
    assert input_time(_NOW, 0xFFFFFF00, 0x100) == _NOW - timedelta(milliseconds=512)


def test_input_at_the_last_count_before_the_wrap():
    assert input_time(_NOW, 0xFFFFFFFF, 0) == _NOW - timedelta(milliseconds=1)


def test_the_win32_probe_reads_its_seam_once_per_call():
    reads: list[int] = []

    def read() -> tuple[int, int]:
        reads.append(1)
        return (0xFFFFFF00, 0x100)

    assert Win32Input(read).last_input(_NOW) == _NOW - timedelta(milliseconds=512)
    assert reads == [1]


def test_a_failure_streak_is_logged_once_when_it_starts_and_once_when_it_ends(caplog):
    answers: list[tuple[int, int] | OSError] = [
        (10, 20),
        OSError(5, "GetLastInputInfo failed: Access is denied."),
        OSError(5, "GetLastInputInfo failed: Access is denied."),
        OSError(5, "GetLastInputInfo failed: Access is denied."),
        (30, 40),
        (50, 60),
        OSError(6, "GetLastInputInfo failed: The handle is invalid."),
    ]

    def read() -> tuple[int, int]:
        answer = answers.pop(0)
        if isinstance(answer, OSError):
            raise answer
        return answer

    probe = Win32Input(read)
    with caplog.at_level(logging.INFO, logger=backend.__name__):
        results = [probe.last_input(_NOW) for _ in range(7)]

    assert [result is None for result in results] == [False, True, True, True, False, False, True]
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 3
    assert "last input unreadable" in messages[0] and "Access is denied" in messages[0]
    assert messages[1] == "last input readable again"
    assert "last input unreadable" in messages[2] and "handle is invalid" in messages[2]
    assert [record.levelno for record in caplog.records] == [logging.WARNING, logging.INFO, logging.WARNING]


def test_no_input_reads_nothing():
    assert NoInput().last_input(_NOW) is None


@pytest.mark.parametrize("platform", ["linux", "darwin", "android"])
def test_a_platform_without_a_backend_gets_no_input(platform):
    assert isinstance(backend.input_probe(platform), NoInput)


def test_a_backend_that_cannot_load_gives_no_input(monkeypatch, caplog):
    def broken() -> backend.InputProbe:
        raise ImportError("no user32")

    monkeypatch.setitem(backend._INPUT_REGISTRY, "win32", broken)
    with caplog.at_level(logging.WARNING, logger=backend.__name__):
        assert isinstance(backend.input_probe("win32"), NoInput)
    assert "input probe backend for win32 unavailable" in caplog.text


@pytest.mark.skipif(sys.platform != "win32", reason="GetLastInputInfo is Windows only")
def test_the_real_call_returns_two_32_bit_counts_without_elevation():
    from timeexisting.presence import input_win32

    assert input_win32.LASTINPUTINFO.dwTime.offset == 4
    last, ticks = input_win32.last_input()
    assert 0 <= last <= 0xFFFFFFFF and 0 <= ticks <= 0xFFFFFFFF
    assert isinstance(backend.input_probe("win32"), Win32Input)
