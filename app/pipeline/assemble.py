"""Group raw feed items into publishable Articles.

For an ordinary feed this is a 1:1 mapping. For a Threads/fediverse feed in
merge_self mode it is where a chain of self-replies becomes one article.

The self-thread rules:

  * A post whose reply_to_author_key equals its own author_key is a
    continuation. Walk that chain up to its root; every part with the same root
    is one article.
  * A post replying to *somebody else* is a conversation reply. Without the
    other side of the conversation -- which, for Threads, usually has not
    federated at all -- it reads as a non sequitur, so merge_self mode drops
    it. Use thread_mode=none on a feed where you want every reply.
  * A thread is held open until `thread_grace_minutes` after its newest part.
    Publishing immediately is the thing that goes wrong here: parts 1-3 ship
    to the ereader, get marked read and vanish, and parts 4-6 turn up the next
    day as an orphan with no beginning.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Article, ArticleState, Feed, FeedItem, ThreadMode, utcnow
from ..settings_store import get as setting
from . import clean

log = logging.getLogger(__name__)

TITLE_MAX = 90


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands datetimes back naive; treat stored values as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def derive_title(html: str, fallback: str = "Untitled") -> str:
    """Microblog posts have no title, so make one from the opening words."""
    text = clean.text_of(html).strip()
    if not text:
        return fallback
    text = clean.THREAD_MARKER.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= TITLE_MAX:
        return text
    # Prefer a sentence boundary, else a word boundary.
    window = text[: TITLE_MAX + 30]
    match = re.search(r"^(.{20,%d}?[.!?])\s" % TITLE_MAX, window)
    if match:
        return match.group(1).strip()
    return text[:TITLE_MAX].rsplit(" ", 1)[0].strip() + "…"


def _item_body(item: FeedItem) -> str:
    return item.content_html or item.summary_html or ""


# --- self-thread grouping -------------------------------------------------

def _chain_root(item: FeedItem, by_guid: dict[str, FeedItem]) -> str:
    """Follow self-replies upwards and return the root post's guid."""
    seen: set[str] = set()
    current = item
    while True:
        if current.guid in seen:  # defensive: a cycle should be impossible
            break
        seen.add(current.guid)
        parent_guid = current.reply_to_guid
        if not parent_guid:
            break
        # Only self-replies continue a thread.
        if current.reply_to_author_key != current.author_key:
            break
        parent = by_guid.get(parent_guid)
        if parent is None:
            # The parent never reached us (federation gap, or it predates the
            # follow). This part becomes its own root rather than being lost.
            break
        current = parent
    return current.guid


def _is_reply_to_other(item: FeedItem) -> bool:
    return bool(item.reply_to_guid
                and item.reply_to_author_key
                and item.reply_to_author_key != item.author_key)


def _sort_key(item: FeedItem):
    return (_aware(item.published_at) or _aware(item.fetched_at)
            or datetime.min.replace(tzinfo=timezone.utc), item.id)


def assemble_feed(session: Session, feed: Feed) -> int:
    """Create Articles for this feed's unassigned items. Returns how many."""
    pending_items = list(session.execute(
        select(FeedItem)
        .where(FeedItem.feed_id == feed.id, FeedItem.article_id.is_(None),
               FeedItem.skipped.is_(False))
        .order_by(FeedItem.published_at, FeedItem.id)
    ).scalars())
    if not pending_items:
        return 0

    if feed.thread_mode != ThreadMode.merge_self:
        return _assemble_flat(session, feed, pending_items)
    return _assemble_threads(session, feed, pending_items)


def _assemble_flat(session: Session, feed: Feed, items: list[FeedItem]) -> int:
    made = 0
    for item in items:
        body = _item_body(item)
        article = Article(
            feed_id=feed.id,
            category_id=feed.category_id,
            title=(item.title or derive_title(body)).strip()[:512],
            byline=item.author,
            url=item.url,
            published_at=item.published_at or item.fetched_at,
            body_html=body,
            state=ArticleState.pending,
            part_count=1,
        )
        session.add(article)
        session.flush()
        item.article_id = article.id
        made += 1
    return made


def _assemble_threads(session: Session, feed: Feed, items: list[FeedItem]) -> int:
    grace = timedelta(minutes=setting(session, "thread_grace_minutes"))
    max_parts = setting(session, "thread_max_parts")
    now = utcnow()

    # Resolving a chain needs items we have already filed, not just new ones.
    known = list(session.execute(
        select(FeedItem).where(FeedItem.feed_id == feed.id)
        .order_by(FeedItem.id.desc()).limit(2000)
    ).scalars())
    by_guid = {i.guid: i for i in known}
    for item in items:
        by_guid[item.guid] = item

    groups: dict[str, list[FeedItem]] = {}
    for item in items:
        if _is_reply_to_other(item):
            # Not part of a self-thread and meaningless alone -- flag it so
            # it is not reconsidered on every pass.
            item.skipped = True
            item.skip_reason = "reply to another account"
            continue
        groups.setdefault(_chain_root(item, by_guid), []).append(item)

    made = 0
    for root_guid, parts in groups.items():
        parts.sort(key=_sort_key)
        newest = _sort_key(parts[-1])[0]
        open_until = newest + grace

        article = session.execute(
            select(Article).where(Article.feed_id == feed.id,
                                  Article.thread_key == root_guid)
            .order_by(Article.id.desc())
        ).scalars().first()

        reopened = False
        if article is not None:
            still_open = (article.state == ArticleState.pending
                          and (_aware(article.thread_open_until) or now) > now)
            if still_open and article.part_count + len(parts) <= max_parts:
                # Late parts joining a thread we have not published yet.
                existing = sorted(article.items, key=_sort_key)
                parts = sorted(existing + parts, key=_sort_key)
                reopened = True
            else:
                # The thread already went out. A continuation becomes its own
                # article so the new parts are not silently dropped.
                article = None

        if article is None:
            article = Article(feed_id=feed.id, category_id=feed.category_id,
                              thread_key=root_guid, state=ArticleState.pending)
            session.add(article)
            session.flush()
            made += 1

        body = "\n".join(_item_body(p) for p in parts if _item_body(p).strip())
        if len(parts) > 1:
            body = clean.strip_thread_markers(body)

        first = parts[0]
        article.category_id = feed.category_id
        article.title = (first.title or derive_title(body)).strip()[:512]
        article.byline = first.author
        article.url = first.url
        article.published_at = first.published_at or first.fetched_at
        article.body_html = body
        article.part_count = len(parts)
        article.thread_open_until = open_until
        article.state = ArticleState.pending

        for part in parts:
            part.article_id = article.id

        if reopened:
            log.info("thread %s grew to %d parts, held until %s",
                     root_guid[:40], len(parts), open_until)

    return made


def assemble_all(session: Session) -> int:
    total = 0
    feeds = session.execute(select(Feed).where(Feed.enabled.is_(True))).scalars()
    for feed in feeds:
        try:
            total += assemble_feed(session, feed)
            session.flush()
        except Exception:
            log.exception("assemble failed for feed %s", feed.id)
            session.rollback()
    return total
