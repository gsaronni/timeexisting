"""The event log backend registry (roadmap section 6): which event log this platform has, and the reader that joins the I/O seams to the pure parser.

Windows-only modules are imported lazily, inside the registry entry, so this module imports on every platform. Windows is the only live backend in phase 3; a platform without one (Linux until the Fedora backend, Termux) gets `NoEventLog`, which returns nothing. A query failure is logged and returns nothing: it never stops the collector and never asks for elevation.
"""

import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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


def _windows() -> EventLogReader:
    from timeexisting.presence import eventlog_win32

    return EventLog(eventlog_win32.query, eventlog_win32.probe, eventlog_win32.current_user_sid)


# Keyed by `sys.platform`. The Fedora D-Bus backend (section 18) goes here when it can be live-checked.
_REGISTRY: dict[str, Callable[[], EventLogReader]] = {"win32": _windows}


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
