import logging
from logging.handlers import RotatingFileHandler

import pytest

from timeexisting import logging_setup, paths


def _our_handlers() -> list[RotatingFileHandler]:
    return [handler for handler in logging.getLogger().handlers if isinstance(handler, RotatingFileHandler)]


@pytest.mark.parametrize("role", ["collector", "viewer"])
def test_each_role_writes_its_own_file(role):
    logging_setup.configure(role)
    logging.getLogger("timeexisting.test").info("heartbeat written")
    logging_setup.reset()

    line = paths.log_file(role).read_text(encoding="utf-8").strip()
    assert line.endswith(f"{role} INFO timeexisting.test: heartbeat written")
    assert line[:4].isdigit()  # asctime leads
    assert [path.name for path in paths.log_dir().iterdir()] == [paths.log_file(role).name]


def test_roles_never_share_a_file():
    logging_setup.configure("collector")
    logging.getLogger("timeexisting.test").info("from the collector")
    logging_setup.configure("viewer")
    logging.getLogger("timeexisting.test").info("from the viewer")
    logging_setup.reset()

    collector = paths.log_file("collector").read_text(encoding="utf-8")
    viewer = paths.log_file("viewer").read_text(encoding="utf-8")
    assert "from the collector" in collector
    assert "from the viewer" not in collector
    assert "from the viewer" in viewer
    assert "from the collector" not in viewer


def test_configure_uses_the_agreed_rotation():
    logging_setup.configure("viewer")
    (handler,) = _our_handlers()
    assert handler.maxBytes == 1_000_000
    assert handler.backupCount == 3
    assert handler.baseFilename == str(paths.log_file("viewer"))


def test_rotation_stays_within_the_role():
    logging_setup.configure("collector")
    (handler,) = _our_handlers()
    handler.maxBytes = 200
    log = logging.getLogger("timeexisting.test")
    for index in range(40):
        log.info("line %d padded to force a rollover quickly", index)
    logging_setup.reset()

    names = sorted(path.name for path in paths.log_dir().iterdir())
    base = paths.log_file("collector").name
    assert names == [base, f"{base}.1", f"{base}.2", f"{base}.3"]


def test_configure_twice_does_not_stack_handlers():
    logging_setup.configure("viewer")
    logging_setup.configure("viewer")
    assert len(_our_handlers()) == 1


def test_failed_emit_is_silent():
    logging_setup.configure("viewer")
    assert logging.raiseExceptions is False


def test_nothing_reaches_stdout_or_stderr(capfd):
    logging_setup.configure("viewer")
    logging.getLogger("timeexisting.test").warning("should only be in the file")
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert not any(type(handler) is logging.StreamHandler for handler in logging.getLogger().handlers)


def test_reset_closes_and_removes_the_handler():
    logging_setup.configure("viewer")
    (handler,) = _our_handlers()
    logging_setup.reset()
    assert _our_handlers() == []
    assert handler.stream is None
