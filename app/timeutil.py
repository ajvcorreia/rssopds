"""Display timezone handling.

Everything is *stored* in UTC and that does not change -- timestamps in the
database are unambiguous, comparable, and immune to DST. What was missing is
the conversion on the way out: the web UI had a filter called "localtime" that
did no converting at all, so a container running on UTC showed every time in
UTC while the person reading it was four hours ahead.

The zone is a runtime setting rather than a container TZ variable, because it
is a presentation choice for whoever reads the web UI, not a property of the
host. It is cached here so a Jinja filter can reach it without a database
round trip on every timestamp.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

_tz: timezone | ZoneInfo = timezone.utc
_name = "UTC"


def set_display_timezone(name: str) -> str:
    """Set the zone used for display. Returns the name actually applied."""
    global _tz, _name
    wanted = (name or "UTC").strip() or "UTC"
    try:
        _tz = ZoneInfo(wanted)
        _name = wanted
    except (ZoneInfoNotFoundError, ValueError, OSError):
        log.warning("unknown timezone %r; falling back to UTC", wanted)
        _tz = timezone.utc
        _name = "UTC"
    return _name


def display_timezone_name() -> str:
    return _name


def as_utc(dt: datetime | None) -> datetime | None:
    """Treat a naive datetime as UTC, which is how they are stored."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_display(dt: datetime | None) -> datetime | None:
    """Convert a stored (UTC) timestamp into the configured display zone."""
    aware = as_utc(dt)
    return None if aware is None else aware.astimezone(_tz)


def format_display(dt: datetime | None, fmt: str = "%d %b %H:%M") -> str:
    local = to_display(dt)
    return local.strftime(fmt) if local else "—"


def now() -> datetime:
    """Current time in the display zone.

    Used for things a reader sees as a date -- edition titles, cover
    subtitles -- so that a book built at 22:00 local is not dated yesterday.
    """
    return datetime.now(_tz)
