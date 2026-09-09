"""The four background jobs, each writing a JobRun row for the status page."""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import session_scope
from .models import Feed, FeedItem, JobRun, JobStatus, utcnow
from .pipeline import assemble, editions, process
from .sources import SourceError, source_for

log = logging.getLogger(__name__)


@contextmanager
def _job(session: Session, name: str, feed_id: int | None = None) -> Iterator[JobRun]:
    run = JobRun(job=name, feed_id=feed_id, status=JobStatus.running)
    session.add(run)
    session.commit()
    started = time.monotonic()
    try:
        yield run
        run.status = JobStatus.ok
    except Exception as exc:
        run.status = JobStatus.error
        run.message = str(exc)[:1000]
        log.exception("job %s failed", name)
    finally:
        run.duration_s = round(time.monotonic() - started, 2)
        run.finished_at = utcnow()
        session.commit()


def poll_feed(session: Session, feed: Feed) -> int:
    """Fetch one feed and store any items we have not seen. Returns new count."""
    source = source_for(feed)
    result = source.fetch(feed, session)

    feed.last_polled_at = utcnow()
    if result.not_modified:
        feed.last_ok_at = feed.last_polled_at
        feed.last_error = None
        feed.consecutive_failures = 0
        return 0

    existing = {
        guid for (guid,) in session.execute(
            select(FeedItem.guid).where(FeedItem.feed_id == feed.id)
        )
    }

    new_count = 0
    for raw in result.items:
        if raw.guid in existing:
            continue
        session.add(FeedItem(
            feed_id=feed.id,
            guid=raw.guid,
            url=raw.url,
            title=raw.title,
            author=raw.author,
            author_key=raw.author_key,
            summary_html=raw.summary_html,
            content_html=raw.content_html,
            image_url=raw.image_url,
            published_at=raw.published_at,
            reply_to_guid=raw.reply_to_guid,
            reply_to_author_key=raw.reply_to_author_key,
        ))
        existing.add(raw.guid)
        new_count += 1

    try:
        session.flush()
    except IntegrityError:
        # Two pollers raced on the same feed; the unique constraint did its job.
        session.rollback()
        return 0

    if result.etag is not None:
        feed.etag = result.etag
    if result.last_modified is not None:
        feed.last_modified = result.last_modified
    if result.cursor is not None:
        feed.cursor = result.cursor

    feed.last_ok_at = feed.last_polled_at
    feed.last_error = None
    feed.consecutive_failures = 0
    return new_count


def run_poll(feed_id: int) -> None:
    with session_scope() as session:
        feed = session.get(Feed, feed_id)
        if feed is None or not feed.enabled:
            return
        with _job(session, "poll", feed_id) as run:
            try:
                run.items_new = poll_feed(session, feed)
                run.message = f"{run.items_new} new item(s)"
            except SourceError as exc:
                feed.last_polled_at = utcnow()
                feed.last_error = str(exc)[:1000]
                feed.consecutive_failures += 1
                raise


def run_poll_all() -> None:
    with session_scope() as session:
        ids = [i for (i,) in session.execute(
            select(Feed.id).where(Feed.enabled.is_(True))
        )]
    for feed_id in ids:
        run_poll(feed_id)


def run_assemble() -> None:
    with session_scope() as session:
        with _job(session, "assemble") as run:
            run.articles_made = assemble.assemble_all(session)
            run.message = f"{run.articles_made} article(s) assembled"


def run_process(limit: int = 25) -> None:
    with session_scope() as session:
        with _job(session, "process") as run:
            done = process.process_batch(session, limit)
            run.articles_made = done
            run.message = f"{done} article(s) processed"


def run_build() -> None:
    with session_scope() as session:
        with _job(session, "build") as run:
            confirmed = editions.confirm_due_deliveries(session)
            session.commit()
            built = editions.build_all(session)
            run.message = (f"{built} edition(s) built, "
                           f"{confirmed} delivery(ies) confirmed")


def run_pipeline_once() -> None:
    """Poll, assemble, process and build in order -- the 'Run now' button."""
    run_poll_all()
    run_assemble()
    run_process(limit=100)
    run_build()
