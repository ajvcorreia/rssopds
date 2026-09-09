"""Assemble ready articles into per-category EPUB editions, and track delivery.

Delivery is the awkward half. OPDS has no "the download worked" callback, so
the only evidence available is how many bytes of the file actually made it out
of the socket. Readers such as KOReader fetch with Range requests, so a single
206 proves nothing; `Delivery.covered` therefore accumulates merged byte
intervals across every request for that edition, and the edition counts as
delivered only once the union covers essentially the whole file.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import config
from ..models import (
    Article, ArticleState, Category, Delivery, Edition, EditionArticle,
    EditionState, utcnow,
)
from ..settings_store import get as setting
from .. import timeutil
from . import covers, epub

log = logging.getLogger(__name__)

Interval = tuple[int, int]  # [start, end)


# --- byte coverage --------------------------------------------------------

def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def covered_bytes(intervals: list[Interval]) -> int:
    return sum(end - start for start, end in merge_intervals(intervals))


def record_bytes(session: Session, delivery: Delivery, start: int, end: int,
                 total: int) -> bool:
    """Fold one served byte range in. Returns True if the file is now complete."""
    try:
        intervals = [tuple(i) for i in json.loads(delivery.covered or "[]")]
    except (ValueError, TypeError):
        intervals = []
    intervals.append((start, end))
    merged = merge_intervals(intervals)

    delivery.covered = json.dumps(merged)
    delivery.bytes_sent = covered_bytes(merged)

    threshold = setting(session, "delivery_min_fraction")
    complete = total > 0 and delivery.bytes_sent >= total * threshold
    if complete and not delivery.complete:
        delivery.complete = True
        delivery.completed_at = utcnow()
    return complete


# --- building -------------------------------------------------------------

def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


TITLE_PLACEHOLDERS = ("{category}", "{date}", "{time}", "{datetime}",
                      "{n}", "{count}")

# A title without one of these repeats for every edition built on the same
# day. That matters more than it looks: ereaders name the downloaded file
# after the OPDS title, so repeated titles silently overwrite yesterday's
# book on the device.
UNIQUE_PLACEHOLDERS = ("{n}", "{time}", "{datetime}")


def format_title(template: str, *, category: str, when: datetime,
                 count: int, number: int) -> str:
    try:
        return template.format(
            category=category,
            date=when.strftime("%d %b %Y"),
            time=when.strftime("%H:%M"),
            datetime=when.strftime("%d %b %Y %H:%M"),
            n=number,
            count=count,
        )
    except (KeyError, IndexError, ValueError):
        # A bad template must not stop editions being built.
        log.warning("edition_title_format %r is invalid; using the default",
                    template)
        return (f"{category} No. {number} - {when:%d %b %Y}")


def title_is_unique_per_edition(template: str) -> bool:
    return any(token in template for token in UNIQUE_PLACEHOLDERS)


def ready_articles(session: Session, category_id: int, limit: int) -> list[Article]:
    return list(session.execute(
        select(Article)
        .where(Article.category_id == category_id,
               Article.state == ArticleState.ready)
        .order_by(Article.published_at.desc().nulls_last(), Article.id.desc())
        .limit(limit)
    ).scalars())


def was_fully_downloaded(edition: Edition) -> bool:
    return any(d.complete for d in edition.deliveries)


def _supersede(session: Session, edition: Edition) -> bool:
    """Retire an undelivered edition and return its articles to the pool.

    An edition the reader has *already* fully downloaded is confirmed instead
    of retired. Superseding it would send its articles back to the pool and
    they would turn up again, unread, in the next book -- which is exactly
    what happened to four editions before this check existed. It can confirm a
    download slightly before the grace period is up, but re-serving something
    already on the device is the worse outcome.

    Returns True when the edition was actually superseded.
    """
    if was_fully_downloaded(edition):
        log.info("edition %s was fully downloaded; confirming instead of "
                 "superseding it", edition.id)
        mark_delivered(session, edition)
        return False

    for link in edition.articles:
        if link.article and link.article.state == ArticleState.published:
            link.article.state = ArticleState.ready
    edition.state = EditionState.superseded
    if edition.epub_file:
        (config.epub_dir / edition.epub_file).unlink(missing_ok=True)
        edition.epub_file = None
    return True


def build_category(session: Session, category: Category) -> Edition | None:
    """Build a fresh edition for one category, if it has anything unread."""
    min_articles = setting(session, "build_min_articles")
    max_articles = setting(session, "build_max_articles")

    existing = session.execute(
        select(Edition).where(Edition.category_id == category.id,
                              Edition.state == EditionState.available)
    ).scalars().all()

    fresh = ready_articles(session, category.id, max_articles)
    if not fresh:
        return None
    if existing and len(fresh) < min_articles:
        # Nothing new worth reissuing for; leave the current edition alone.
        return None

    for old in existing:
        _supersede(session, old)
    session.flush()

    # Superseding returned the old edition's articles to `ready`, so re-query
    # to pick up everything that should be in the new book.
    articles = ready_articles(session, category.id, max_articles)
    if len(articles) < min_articles:
        return None

    when = timeutil.now()
    # Issue number continues across superseded and delivered editions, so it
    # never repeats for a category even after a rebuild.
    number = (session.execute(
        select(func.max(Edition.number)).where(Edition.category_id == category.id)
    ).scalar() or 0) + 1

    title = format_title(setting(session, "edition_title_format"),
                         category=category.name, when=when,
                         count=len(articles), number=number)

    # Blank publisher means the category is the author, so an ereader library
    # groups editions by category instead of by this application.
    author = setting(session, "publisher_name").strip() or category.name
    embed_images = bool(setting(session, "images_enabled"))

    uploaded = (config.cover_dir / category.cover_file) if category.cover_file else None
    cover = covers.cover_for(category.name, uploaded, config.cover_dir,
                             when=when, count=len(articles),
                             width=setting(session, "image_max_width"),
                             height=setting(session, "image_max_height"))

    edition = Edition(category_id=category.id, title=title, number=number,
                      state=EditionState.building,
                      article_count=len(articles))
    session.add(edition)
    session.flush()

    filename = f"{category.slug}-no{number:03d}-{when:%Y%m%d-%H%M}.epub"
    out_path = config.epub_dir / filename

    epub.build(title=title, author=author, articles=articles, cover_path=cover,
               image_dir=config.image_dir, out_path=out_path, when=when,
               embed_images=embed_images)

    edition.epub_file = filename
    edition.size_bytes = out_path.stat().st_size
    edition.state = EditionState.available

    for position, article in enumerate(articles):
        session.add(EditionArticle(edition_id=edition.id, article_id=article.id,
                                   position=position))
        article.state = ArticleState.published

    log.info("built edition %s (%s, %d articles, %d bytes)",
             edition.id, category.name, len(articles), edition.size_bytes)
    return edition


def supersede_available(session: Session) -> int:
    """Retire every undelivered edition so the next build regenerates it.

    Used when something that goes *inside* a book has been fixed: the EPUBs
    already on disk still contain the old content, and nothing else would ever
    rebuild them. Articles return to `ready`, so nothing is lost or re-read.
    """
    # Confirm anything already downloaded first, so a pending delivery is not
    # swept away by a rebuild the reader had nothing to do with.
    confirm_due_deliveries(session)

    stale = session.execute(
        select(Edition).where(Edition.state == EditionState.available)
    ).scalars().all()
    retired = sum(1 for edition in stale if _supersede(session, edition))
    if retired:
        log.info("superseded %d undelivered edition(s) for rebuild", retired)
    return retired


def build_all(session: Session) -> int:
    built = 0
    categories = session.execute(
        select(Category).order_by(Category.sort_order, Category.name)
    ).scalars().all()
    for category in categories:
        try:
            if build_category(session, category) is not None:
                built += 1
            session.commit()
        except Exception:
            session.rollback()
            log.exception("edition build failed for category %s", category.id)
    return built


# --- delivery -------------------------------------------------------------

def mark_delivered(session: Session, edition: Edition) -> None:
    """Confirm an edition reached the reader: its articles are now read."""
    if edition.state == EditionState.delivered:
        return
    now = utcnow()
    edition.state = EditionState.delivered
    edition.delivered_at = now
    for link in edition.articles:
        # Only `published` articles -- ones still sitting in this edition. An
        # article put back to `ready` was deliberately taken out of it (someone
        # hit "mark unread"), and a delivery confirmation arriving afterwards
        # must not silently undo that.
        if link.article and link.article.state == ArticleState.published:
            link.article.state = ArticleState.delivered
            link.article.delivered_at = now
    log.info("edition %s delivered; %d articles marked read",
             edition.id, len(edition.articles))
    prune_delivered(session)


def undo_delivery(session: Session, edition: Edition) -> None:
    """Put an edition and its articles back, for a sync that only looked good."""
    edition.state = EditionState.available
    edition.delivered_at = None
    for link in edition.articles:
        if link.article:
            link.article.state = ArticleState.published
            link.article.delivered_at = None
    for delivery in edition.deliveries:
        delivery.complete = False
        delivery.completed_at = None
        delivery.covered = "[]"
        delivery.bytes_sent = 0


def confirm_due_deliveries(session: Session) -> int:
    """Promote complete downloads to delivered, after the confirm delay.

    The delay exists so a sync that completed the HTTP transfer but failed on
    the device can still be undone from the web UI before the articles vanish.
    """
    delay = timedelta(seconds=setting(session, "delivery_confirm_delay_s"))
    cutoff = utcnow() - delay

    rows = session.execute(
        select(Delivery, Edition)
        .join(Edition, Delivery.edition_id == Edition.id)
        .where(Delivery.complete.is_(True),
               Edition.state == EditionState.available)
    ).all()

    confirmed = 0
    for delivery, edition in rows:
        completed = _aware(delivery.completed_at)
        if completed is not None and completed <= cutoff:
            mark_delivered(session, edition)
            confirmed += 1
    return confirmed


def prune_delivered(session: Session) -> None:
    """Delete EPUB files for all but the most recent delivered editions."""
    keep = setting(session, "keep_delivered_editions")

    # The session runs with autoflush off, so epub_file=None set by an earlier
    # call in this same transaction is not yet visible to SQL. Without this
    # flush the isnot(None) filter still matches those rows, and the identity
    # map hands back the object whose epub_file is already None.
    session.flush()

    stale = session.execute(
        select(Edition)
        .where(Edition.state == EditionState.delivered,
               Edition.epub_file.isnot(None))
        .order_by(Edition.delivered_at.desc().nulls_last())
        .offset(keep)
    ).scalars().all()
    for edition in stale:
        if not edition.epub_file:
            continue  # already pruned earlier in this transaction
        (config.epub_dir / edition.epub_file).unlink(missing_ok=True)
        edition.epub_file = None


def orphan_images(session: Session) -> list[Path]:
    """Stored images no live article references any more."""
    referenced: set[str] = set()
    for (body,) in session.execute(
        select(Article.body_html).where(Article.body_html.isnot(None))
    ):
        referenced |= epub._referenced_images(body or "")
    for (name,) in session.execute(
        select(Article.image_file).where(Article.image_file.isnot(None))
    ):
        referenced.add(name)
    return [p for p in config.image_dir.glob("*.jpg") if p.name not in referenced]


def catalog_categories(session: Session) -> list[tuple[Category, Edition]]:
    """Categories that currently have something to read, newest edition each.

    A category with no available edition is simply absent from the catalogue --
    that is what makes empty sections disappear from the ereader.
    """
    newest = (
        select(Edition.category_id.label("cid"),
               func.max(Edition.id).label("eid"))
        .where(Edition.state == EditionState.available,
               Edition.epub_file.isnot(None))
        .group_by(Edition.category_id)
        .subquery()
    )
    rows = session.execute(
        select(Category, Edition)
        .join(newest, newest.c.cid == Category.id)
        .join(Edition, Edition.id == newest.c.eid)
        .order_by(Category.sort_order, Category.name)
    ).all()
    return [(c, e) for c, e in rows]
