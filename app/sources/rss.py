"""Plain RSS/Atom source, via feedparser.

Also covers third-party bridge output (RSS-Bridge and friends) -- from this
app's point of view a bridge is just another feed URL.
"""
from __future__ import annotations

import logging
from calendar import timegm
from datetime import datetime, timezone

import feedparser
import httpx

from ..models import Feed
from ..settings_store import get as setting
from .base import FetchResult, RawItem, SourceError

log = logging.getLogger(__name__)


def _to_dt(struct_time) -> datetime | None:
    if not struct_time:
        return None
    try:
        return datetime.fromtimestamp(timegm(struct_time), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _pick_image(entry) -> str | None:
    """First plausible image URL from the entry's own metadata."""
    for enc in entry.get("enclosures", []) or []:
        if str(enc.get("type", "")).startswith("image/") and enc.get("href"):
            return enc["href"]
    for thumb in entry.get("media_thumbnail", []) or []:
        if thumb.get("url"):
            return thumb["url"]
    for media in entry.get("media_content", []) or []:
        url = media.get("url")
        if url and (str(media.get("type", "")).startswith("image/")
                    or media.get("medium") == "image"):
            return url
    for link in entry.get("links", []) or []:
        if link.get("rel") == "enclosure" and str(link.get("type", "")).startswith("image/"):
            return link.get("href")
    return None


def _entry_html(entry) -> tuple[str | None, str | None]:
    """(summary, full content) as raw HTML strings."""
    summary = entry.get("summary")
    content = None
    blocks = entry.get("content") or []
    if blocks:
        # Prefer the longest content block; some feeds ship several.
        best = max(blocks, key=lambda b: len(b.get("value") or ""))
        content = best.get("value") or None
    return summary, content


class RssSource:
    def fetch(self, feed: Feed, session) -> FetchResult:
        timeout = setting(session, "fetch_timeout_s")
        headers = {"User-Agent": setting(session, "http_user_agent")}
        if feed.etag:
            headers["If-None-Match"] = feed.etag
        if feed.last_modified:
            headers["If-Modified-Since"] = feed.last_modified

        try:
            resp = httpx.get(feed.url, headers=headers, timeout=timeout,
                             follow_redirects=True)
        except httpx.HTTPError as exc:
            raise SourceError(f"fetch failed: {exc}") from exc

        if resp.status_code == 304:
            return FetchResult(not_modified=True, etag=feed.etag,
                               last_modified=feed.last_modified)
        if resp.status_code >= 400:
            raise SourceError(f"HTTP {resp.status_code} from {feed.url}")

        parsed = feedparser.parse(resp.content)
        # bozo just means "not strictly well-formed"; feedparser still recovers
        # most real-world feeds, so only fail when nothing came out of it.
        if parsed.bozo and not parsed.entries:
            raise SourceError(f"unparseable feed: {parsed.get('bozo_exception')}")

        items: list[RawItem] = []
        for entry in parsed.entries[: feed.max_items_per_poll]:
            guid = (entry.get("id") or entry.get("link")
                    or entry.get("title") or "").strip()
            if not guid:
                continue
            summary, content = _entry_html(entry)
            items.append(RawItem(
                guid=guid[:512],
                url=entry.get("link"),
                title=(entry.get("title") or "").strip() or None,
                author=entry.get("author") or None,
                author_key=(entry.get("author") or "").strip().lower() or None,
                summary_html=summary,
                content_html=content,
                image_url=_pick_image(entry),
                published_at=_to_dt(entry.get("published_parsed")
                                    or entry.get("updated_parsed")),
            ))

        return FetchResult(
            items=items,
            etag=resp.headers.get("ETag"),
            last_modified=resp.headers.get("Last-Modified"),
        )
