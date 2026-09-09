"""Source adapter contract.

A source turns a Feed row into a list of RawItem. It does no database work and
no content cleaning -- it only normalises whatever the remote gave us.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session

from ..models import Feed


@dataclass(slots=True)
class RawItem:
    guid: str
    url: str | None = None
    title: str | None = None
    author: str | None = None
    author_key: str | None = None
    summary_html: str | None = None
    content_html: str | None = None
    image_url: str | None = None
    published_at: datetime | None = None
    # Threading, only set by sources that actually know (fediverse does).
    reply_to_guid: str | None = None
    reply_to_author_key: str | None = None


@dataclass(slots=True)
class FetchResult:
    items: list[RawItem] = field(default_factory=list)
    # Conditional-GET / pagination state, written back onto the Feed row.
    etag: str | None = None
    last_modified: str | None = None
    cursor: str | None = None
    # True when the remote said "nothing changed" (HTTP 304).
    not_modified: bool = False


class Source(Protocol):
    def fetch(self, feed: Feed, session: Session) -> FetchResult:
        ...


class SourceError(RuntimeError):
    """Raised for a fetch failure that should be shown on the status page."""
