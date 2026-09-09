"""Take assembled Articles from `pending` to `ready`.

Fetch full text if the feed only gave a teaser, clean the HTML, localise the
images, then mark the article eligible for the next edition.
"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import config
from ..models import Article, ArticleState, SourceKind, utcnow
from ..settings_store import all_settings
from . import clean, extract, images
from .assemble import _aware, derive_title

log = logging.getLogger(__name__)


def due_articles(session: Session, limit: int = 25) -> list[Article]:
    """Pending articles whose self-thread hold-open window has expired."""
    now = utcnow()
    rows = list(session.execute(
        select(Article).where(Article.state == ArticleState.pending)
        .order_by(Article.published_at.asc().nulls_last(), Article.id)
        .limit(limit * 4)
    ).scalars())
    due = [a for a in rows
           if a.thread_open_until is None or _aware(a.thread_open_until) <= now]
    return due[:limit]


def process_article(session: Session, article: Article, cfg: dict) -> None:
    feed = article.feed

    body = article.body_html or ""

    # A merged thread is already the complete text; chasing its first post's
    # permalink would replace it with a single part.
    allow_fetch = bool(feed and feed.extract_fulltext
                       and article.part_count == 1
                       and feed.kind != SourceKind.fediverse)
    extracted = extract.best_body(
        body, article.url,
        allow_fetch=allow_fetch,
        user_agent=cfg["http_user_agent"],
        timeout=cfg["fetch_timeout_s"],
    )
    body = extracted.body_html

    if feed and feed.clean_with_ai:
        body, cleaned_by = clean.ai_clean(
            body,
            base_url=cfg["ollama_url"],
            model=cfg["ollama_model"],
            timeout=cfg["ollama_timeout_s"],
            num_ctx=cfg["ollama_num_ctx"],
            min_retain=cfg["ai_min_retain_ratio"],
            keep_alive=str(cfg["ollama_keep_alive"]),
        )
    else:
        body, cleaned_by = clean.sanitize(body), "rules"

    if cfg["simplify_symbols"]:
        # After cleaning, before images: the substitution is textual and must
        # not disturb the img tags the next step rewrites.
        body = clean.simplify_symbols(body)
        if article.title:
            article.title = clean.simplify_symbols_text(article.title).strip()

    hero: str | None = None
    # The global switch wins: a reader that cannot show images should not make
    # the server fetch and rescale them either.
    want_images = bool(cfg["images_enabled"]) and feed and feed.include_images
    if want_images:
        body, stored = images.rewrite_body_images(
            body, config.image_dir,
            user_agent=cfg["http_user_agent"],
            timeout=cfg["fetch_timeout_s"],
            max_width=cfg["image_max_width"],
            max_height=cfg["image_max_height"],
            quality=cfg["image_quality"],
            grayscale=bool(cfg["image_grayscale"]),
        )
        hero = stored[0] if stored else None

        if hero is None:
            # Nothing inline. Prefer the page's own lead image over the feed's
            # thumbnail: feed thumbnails are routinely 200-300px wide, which
            # looks like a postage stamp on an e-ink screen.
            for candidate in (
                extracted.hero_url,
                next((i.image_url for i in article.items if i.image_url), None),
            ):
                if not candidate:
                    continue
                hero = images.download(
                    candidate, config.image_dir,
                    user_agent=cfg["http_user_agent"],
                    timeout=cfg["fetch_timeout_s"],
                    max_width=cfg["image_max_width"],
                    max_height=cfg["image_max_height"],
                    quality=cfg["image_quality"],
                    grayscale=bool(cfg["image_grayscale"]),
                )
                if hero:
                    break
    else:
        body = clean.strip_images(body)

    article.body_html = body
    article.image_file = hero
    article.word_count = clean.word_count(body)
    article.cleaned_by = cleaned_by
    article.error = None

    if not article.title.strip():
        article.title = derive_title(body)

    if article.word_count == 0:
        # Nothing survived cleaning -- an image-only post, or a dead link.
        article.state = ArticleState.skipped
        article.error = "no text content after cleaning"
    else:
        article.state = ArticleState.ready


def process_batch(session: Session, limit: int = 25) -> int:
    """Process up to `limit` articles, within a wall-clock budget.

    The budget matters because the scheduler runs this job with
    max_instances=1: if one run stays busy for an hour -- which 25 articles
    against a slow model easily can -- every later run is skipped and the
    pipeline stops dead. Stopping early simply leaves the rest for next time.
    """
    cfg = all_settings(session)
    budget = max(30, int(cfg["process_max_seconds"]))
    deadline = time.monotonic() + budget
    done = 0
    for article in due_articles(session, limit):
        if time.monotonic() >= deadline:
            log.info("processing budget of %ss used up after %d article(s); "
                     "the rest continue next run", budget, done)
            break
        try:
            process_article(session, article, cfg)
            session.commit()
            done += 1
        except Exception as exc:
            session.rollback()
            log.exception("processing failed for article %s", article.id)
            fresh = session.get(Article, article.id)
            if fresh is not None:
                fresh.state = ArticleState.failed
                fresh.error = str(exc)[:500]
                session.commit()
    return done
