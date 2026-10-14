"""The presence backend registry (roadmap section 6): which event log and which session probe this platform has, and the readers that join the I/O seams to the pure parser.

Windows-only modules are imported lazily, inside the registry entries, so this module imports on every platform. Windows is the only live backend in phase 3; a platform without one (Linux until the Fedora backend, Termux) gets `NoEventLog` and `NoSessionProbe`, which return nothing. A query or probe failure is logged and returns nothing: it never stops the collector and never asks for elevation.
"""

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from timeexisting.presence.eventlog import (
    SYSTEM_CHANNEL,
    WINLOGON_CHANNEL,
    Boundary,
    EventLogFormatError,
    EventLogUnavailable,
    SessionRecord,
    SystemRecord,
    boundaries,
    parse_system,
    parse_winlogon,
    system_xpath,
    winlogon_xpath,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ChannelCheck:
    """Whether each channel could be queried at the startup self-check."""

    system: bool
    winlogon: bool


class EventLogReader(Protocol):
    def check(self) -> ChannelCheck | None:
        """Query each channel once. `None` when the platform has no event log at all."""
        ...

    def system_boundaries(self, since: datetime, until: datetime) -> list[Boundary]:
        """The System log's machine boundaries from `since`, read up to `until` (`eventlog.boundaries`)."""
        ...

    def session_records(self, since: datetime) -> list[SessionRecord]:
        """This user's Winlogon/Operational lock and unlock records from `since`."""
        ...


class NoEventLog:
    """A platform without an event log backend: nothing to check, nothing to read."""

    def check(self) -> ChannelCheck | None:
        return None

    def system_boundaries(self, since: datetime, until: datetime) -> list[Boundary]:  # noqa: ARG002
        return []

    def session_records(self, since: datetime) -> list[SessionRecord]:  # noqa: ARG002
        return []


class EventLog:
    """The Windows event log through three seams: `query(channel, xpath)` returning each record's XML, `probe(channel)`, and `user_sid()`. Each raises `EventLogUnavailable` on failure, which is logged here and read as nothing."""

    def __init__(
        self,
        query: Callable[[str, str], list[str]],
        probe: Callable[[str], None],
        user_sid: Callable[[], str],
    ) -> None:
        self._query = query
        self._probe = probe
        self._user_sid = user_sid

    def _readable(self, channel: str) -> bool:
        try:
            self._probe(channel)
        except EventLogUnavailable as error:
            logger.warning("event log channel unreadable: %s", error)
            return False
        return True

    def check(self) -> ChannelCheck | None:
        return ChannelCheck(system=self._readable(SYSTEM_CHANNEL), winlogon=self._readable(WINLOGON_CHANNEL))

    def _records(self, channel: str, xpath: str) -> list[str]:
        try:
            return self._query(channel, xpath)
        except EventLogUnavailable as error:
            logger.warning("event log query failed, reading nothing: %s", error)
            return []

    def system_boundaries(self, since: datetime, until: datetime) -> list[Boundary]:
        records: list[SystemRecord] = []
        for xml in self._records(SYSTEM_CHANNEL, system_xpath(since)):
            try:
                record = parse_system(xml)
            except EventLogFormatError as error:
                logger.warning("skipping a malformed System record: %s", error)
                continue
            if record is not None:
                records.append(record)
        return boundaries(records, until=until)

    def session_records(self, since: datetime) -> list[SessionRecord]:
        try:
            sid = self._user_sid()
        except EventLogUnavailable as error:
            logger.warning("no user SID, reading no Winlogon records: %s", error)
            return []
        records: list[SessionRecord] = []
        for xml in self._records(WINLOGON_CHANNEL, winlogon_xpath(since)):
            try:
                record = parse_winlogon(xml, sid)
            except EventLogFormatError as error:
                logger.warning("skipping a malformed Winlogon record: %s", error)
                continue
            if record is not None:
                records.append(record)
        return sorted(records, key=lambda record: record.time)


class SessionState(StrEnum):
    """What the session probe sees, and what the collector's checkpoint holds as the last known session state."""

    LOCKED = "locked"
    UNLOCKED = "unlocked"


class SessionProbe(Protocol):
    def state(self) -> SessionState | None:
        """The session's state right now, or `None` when it cannot be told."""
        ...

    def logon_time(self) -> datetime | None:
        """When this session was logged on, aware UTC, or `None` when it cannot be read."""
        ...


class NoSessionProbe:
    """A platform without a session probe: the state is never known, so the collector neither probes at start nor reconciles."""

    def state(self) -> SessionState | None:
        return None

    def logon_time(self) -> datetime | None:
        return None


class Win32Session:
    """The Windows session probe through two seams over the same `WTSQuerySessionInformation` record (`WTSSessionInfoEx`): `session_state()`, the lock state from `SessionFlags` or `None` when WTS reports it unknown, and `logon_time()`, the session's logon time. Each raises `OSError` on failure, which is logged here and read as `None`.

    `SessionFlags`, not `OpenInputDesktop`: on the work laptop the input desktop opens behind the lock screen (roadmap section 6). The collector still writes nothing on a single reading (the two-tick rule).
    """

    def __init__(
        self,
        session_state: Callable[[], SessionState | None],
        logon_time: Callable[[], datetime | None],
    ) -> None:
        self._session_state = session_state
        self._logon_time = logon_time

    def state(self) -> SessionState | None:
        try:
            return self._session_state()
        except OSError as error:
            logger.warning("session probe failed, state unknown: %s", error)
            return None

    def logon_time(self) -> datetime | None:
        try:
            return self._logon_time()
        except OSError as error:
            logger.warning("session logon time unreadable: %s", error)
            return None


def _windows() -> EventLogReader:
    from timeexisting.presence import eventlog_win32

    return EventLog(eventlog_win32.query, eventlog_win32.probe, eventlog_win32.current_user_sid)


def _windows_session() -> SessionProbe:
    from timeexisting.presence import desktop_win32

    return Win32Session(desktop_win32.session_state, desktop_win32.logon_time)


# Keyed by `sys.platform`. The Fedora D-Bus backend (section 18) goes here when it can be live-checked.
_REGISTRY: dict[str, Callable[[], EventLogReader]] = {"win32": _windows}
_SESSION_REGISTRY: dict[str, Callable[[], SessionProbe]] = {"win32": _windows_session}


def event_log(platform: str = sys.platform) -> EventLogReader:
    """The event log reader for `platform`, or `NoEventLog` when it has no backend or its backend cannot load."""
    factory = _REGISTRY.get(platform)
    if factory is None:
        return NoEventLog()
    try:
        return factory()
    except ImportError as error:
        logger.warning("event log backend for %s unavailable: %s", platform, error)
        return NoEventLog()


def session_probe(platform: str = sys.platform) -> SessionProbe:
    """The session probe for `platform`, or `NoSessionProbe` when it has no backend or its backend cannot load."""
    factory = _SESSION_REGISTRY.get(platform)
    if factory is None:
        return NoSessionProbe()
    try:
        return factory()
    except (ImportError, OSError) as error:
        logger.warning("session probe backend for %s unavailable: %s", platform, error)
        return NoSessionProbe()
