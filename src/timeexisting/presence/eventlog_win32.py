"""The thin I/O half of the event log reader (roadmap section 6): the only code that calls `EvtQuery`. Imported only on Windows, through `presence/backend.py`.

Every function here is a seam: tests replace them, and none of them parses a record. Nothing here asks for elevation; a channel the user cannot read raises `EventLogUnavailable`, which the caller logs.
"""

import pywintypes
import win32api
import win32evtlog
import win32security

from timeexisting.presence.eventlog import EventLogUnavailable

_BATCH = 64
_FLAGS = win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryForwardDirection


def query(channel: str, xpath: str) -> list[str]:
    """Every record in `channel` matching `xpath`, oldest first, each rendered as event XML."""
    rendered: list[str] = []
    try:
        handle = win32evtlog.EvtQuery(channel, _FLAGS, xpath)
        while events := win32evtlog.EvtNext(handle, _BATCH):
            rendered.extend(win32evtlog.EvtRender(event, win32evtlog.EvtRenderEventXml) for event in events)
    except pywintypes.error as error:
        raise EventLogUnavailable(
            f"{channel}: {error.funcname} failed, {error.winerror} {error.strerror}"
        ) from error
    return rendered


def first(channel: str, xpath: str) -> str | None:
    """The oldest record in `channel` matching `xpath`, rendered as event XML, or `None` when nothing matches. Reads one record, not the channel."""
    try:
        handle = win32evtlog.EvtQuery(channel, _FLAGS, xpath)
        events = win32evtlog.EvtNext(handle, 1)
        return win32evtlog.EvtRender(events[0], win32evtlog.EvtRenderEventXml) if events else None
    except pywintypes.error as error:
        raise EventLogUnavailable(
            f"{channel}: {error.funcname} failed, {error.winerror} {error.strerror}"
        ) from error


def probe(channel: str) -> None:
    """Open a query on `channel` and read nothing. An unreadable or absent channel fails here: access denied (5) or channel not found (15007)."""
    try:
        win32evtlog.EvtQuery(channel, _FLAGS, "*")
    except pywintypes.error as error:
        raise EventLogUnavailable(
            f"{channel}: {error.funcname} failed, {error.winerror} {error.strerror}"
        ) from error


def current_user_sid() -> str:
    """The string SID of the user this process runs as, from its own token. Raises `EventLogUnavailable` if the token cannot be read."""
    try:
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        sid, _attributes = win32security.GetTokenInformation(token, win32security.TokenUser)
        return win32security.ConvertSidToStringSid(sid)
    except pywintypes.error as error:
        raise EventLogUnavailable(
            f"user SID: {error.funcname} failed, {error.winerror} {error.strerror}"
        ) from error
