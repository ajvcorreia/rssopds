"""Database schema.

The important shape here is the two-level split between what a feed *emitted*
and what we *publish*:

    FeedItem  (one row per feed GUID, raw)
        |  N:1
    Article   (the publishable unit -- a lone post, or a merged self-thread)

Read/delivered state lives on Article, never on FeedItem, because a Threads
self-thread is several GUIDs but exactly one thing the reader should see once.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Float, ForeignKey, Index, Integer, String,
    Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SourceKind(str, enum.Enum):
    rss = "rss"              # anything feedparser understands (RSS/Atom/bridge output)
    fediverse = "fediverse"  # Mastodon-compatible API -- this is how Threads is read


class ThreadMode(str, enum.Enum):
    none = "none"              # one feed item == one article
    merge_self = "merge_self"  # chain self-replies into a single article


class ArticleState(str, enum.Enum):
    pending = "pending"      # assembled, not yet processed
    ready = "ready"          # cleaned, has image, eligible for an edition
    published = "published"  # sitting in an edition that has not been delivered
    delivered = "delivered"  # confirmed on the ereader; never shown again
    failed = "failed"
    skipped = "skipped"      # user hid it from the web UI


class EditionState(str, enum.Enum):
    building = "building"
    available = "available"
    delivered = "delivered"
    superseded = "superseded"


class JobStatus(str, enum.Enum):
    running = "running"
    ok = "ok"
    error = "error"


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), nullable=False, unique=True)
    slug = Column(String(120), nullable=False, unique=True)
    # Relative to config.cover_dir. NULL means "generate one".
    cover_file = Column(String(255))
    sort_order = Column(Integer, default=100, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    feeds = relationship("Feed", back_populates="category")


class Feed(Base):
    __tablename__ = "feeds"

    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    kind = Column(Enum(SourceKind), default=SourceKind.rss, nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id", ondelete="SET NULL"))
    enabled = Column(Boolean, default=True, nullable=False)

    # rss: the feed URL. fediverse: the instance base URL.
    url = Column(String(1024), nullable=False, default="")

    # -- fediverse / Threads only -------------------------------------------
    # A Mastodon app token belonging to the bot account that follows the
    # Threads handles. fedi_list_id scopes polling to one Mastodon list.
    fedi_token = Column(String(255))
    fedi_list_id = Column(String(64))
    # Optional filter: only keep posts from these acct handles, comma separated
    # (e.g. "someone@threads.net"). Blank keeps the whole list timeline.
    fedi_accounts = Column(Text, default="")
    # Drop boosts/reblogs -- almost always noise for a reading digest.
    fedi_skip_reblogs = Column(Boolean, default=True, nullable=False)

    # -- processing options --------------------------------------------------
    thread_mode = Column(Enum(ThreadMode), default=ThreadMode.none, nullable=False)
    extract_fulltext = Column(Boolean, default=True, nullable=False)
    clean_with_ai = Column(Boolean, default=True, nullable=False)
    include_images = Column(Boolean, default=True, nullable=False)
    poll_interval_min = Column(Integer, default=30, nullable=False)
    max_items_per_poll = Column(Integer, default=50, nullable=False)

    # -- polling state -------------------------------------------------------
    etag = Column(String(255))
    last_modified = Column(String(255))
    cursor = Column(String(128))  # fediverse since_id
    last_polled_at = Column(DateTime)
    last_ok_at = Column(DateTime)
    last_error = Column(Text)
    consecutive_failures = Column(Integer, default=0, nullable=False)

    created_at = Column(DateTime, default=utcnow, nullable=False)

    category = relationship("Category", back_populates="feeds")
    items = relationship("FeedItem", back_populates="feed", cascade="all, delete-orphan")


class FeedItem(Base):
    """One raw entry as the source gave it to us."""

    __tablename__ = "feed_items"
    __table_args__ = (
        UniqueConstraint("feed_id", "guid", name="uq_feed_guid"),
        Index("ix_items_unassigned", "feed_id", "article_id"),
    )

    id = Column(Integer, primary_key=True)
    feed_id = Column(Integer, ForeignKey("feeds.id", ondelete="CASCADE"), nullable=False)
    guid = Column(String(512), nullable=False)

    url = Column(String(1024))
    title = Column(String(512))
    author = Column(String(255))
    # Stable per-source identity of the author, used to decide whether a reply
    # is a self-reply. For fediverse this is the numeric account id.
    author_key = Column(String(128))

    summary_html = Column(Text)
    content_html = Column(Text)
    image_url = Column(String(1024))

    published_at = Column(DateTime)
    fetched_at = Column(DateTime, default=utcnow, nullable=False)

    # Threading, populated by the fediverse source. Exact, not heuristic.
    reply_to_guid = Column(String(512))
    reply_to_author_key = Column(String(128))

    article_id = Column(Integer, ForeignKey("articles.id", ondelete="SET NULL"))
    # Deliberately not publishable (e.g. a reply to somebody else in
    # merge_self mode). Distinct from article_id IS NULL, which means
    # "not looked at yet".
    skipped = Column(Boolean, default=False, nullable=False)
    skip_reason = Column(String(120))

    feed = relationship("Feed", back_populates="items")
    article = relationship("Article", back_populates="items")


class Article(Base):
    """The unit the reader sees: one EPUB chapter."""

    __tablename__ = "articles"
    __table_args__ = (Index("ix_articles_state", "state", "category_id"),)

    id = Column(Integer, primary_key=True)
    feed_id = Column(Integer, ForeignKey("feeds.id", ondelete="CASCADE"), nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id", ondelete="SET NULL"))

    title = Column(String(512), nullable=False, default="")
    byline = Column(String(255))
    url = Column(String(1024))
    published_at = Column(DateTime)

    body_html = Column(Text, default="")
    image_file = Column(String(255))  # relative to config.image_dir
    word_count = Column(Integer, default=0, nullable=False)

    state = Column(Enum(ArticleState), default=ArticleState.pending, nullable=False)
    error = Column(Text)

    # Self-thread grouping. thread_key is the GUID of the root post; the group
    # is held open until thread_open_until passes, so a thread that is still
    # being written does not ship half-finished.
    thread_key = Column(String(512))
    thread_open_until = Column(DateTime)
    part_count = Column(Integer, default=1, nullable=False)

    cleaned_by = Column(String(64))  # model name, or "rules" when AI was skipped
    created_at = Column(DateTime, default=utcnow, nullable=False)
    delivered_at = Column(DateTime)

    items = relationship("FeedItem", back_populates="article")
    category = relationship("Category")
    feed = relationship("Feed")


class Edition(Base):
    """One EPUB: a category's unread articles, frozen at build time."""

    __tablename__ = "editions"

    id = Column(Integer, primary_key=True)
    category_id = Column(Integer, ForeignKey("categories.id", ondelete="CASCADE"),
                         nullable=False)
    # Per-category issue number, like a magazine. Exists so the edition title
    # can be made unique: readers name the downloaded file from the OPDS
    # title, so two editions sharing one would overwrite each other on device.
    number = Column(Integer, default=1, nullable=False)
    title = Column(String(255), nullable=False)
    epub_file = Column(String(255))  # relative to config.epub_dir
    size_bytes = Column(Integer, default=0, nullable=False)
    article_count = Column(Integer, default=0, nullable=False)
    state = Column(Enum(EditionState), default=EditionState.building, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    delivered_at = Column(DateTime)

    category = relationship("Category")
    articles = relationship("EditionArticle", cascade="all, delete-orphan",
                            back_populates="edition")
    deliveries = relationship("Delivery", cascade="all, delete-orphan",
                              back_populates="edition")


class EditionArticle(Base):
    __tablename__ = "edition_articles"
    __table_args__ = (
        UniqueConstraint("edition_id", "article_id", name="uq_edition_article"),
    )

    id = Column(Integer, primary_key=True)
    edition_id = Column(Integer, ForeignKey("editions.id", ondelete="CASCADE"),
                        nullable=False)
    article_id = Column(Integer, ForeignKey("articles.id", ondelete="CASCADE"),
                        nullable=False)
    position = Column(Integer, default=0, nullable=False)

    edition = relationship("Edition", back_populates="articles")
    article = relationship("Article")


class Delivery(Base):
    """A download attempt of an edition's EPUB.

    OPDS gives us no success callback, so completion is inferred from bytes
    actually written to the socket. `covered` holds a JSON list of merged byte
    intervals, because readers such as KOReader fetch with Range requests and a
    single 206 is not proof the whole book arrived.
    """

    __tablename__ = "deliveries"

    id = Column(Integer, primary_key=True)
    edition_id = Column(Integer, ForeignKey("editions.id", ondelete="CASCADE"),
                        nullable=False)
    user_agent = Column(String(255))
    client_ip = Column(String(64))
    covered = Column(Text, default="[]")
    bytes_sent = Column(Integer, default=0, nullable=False)
    complete = Column(Boolean, default=False, nullable=False)
    started_at = Column(DateTime, default=utcnow, nullable=False)
    completed_at = Column(DateTime)

    edition = relationship("Edition", back_populates="deliveries")


class JobRun(Base):
    """Status-page history. One row per poll/assemble/process/build run."""

    __tablename__ = "job_runs"
    __table_args__ = (Index("ix_jobruns_started", "started_at"),)

    id = Column(Integer, primary_key=True)
    job = Column(String(32), nullable=False)
    feed_id = Column(Integer, ForeignKey("feeds.id", ondelete="CASCADE"))
    status = Column(Enum(JobStatus), default=JobStatus.running, nullable=False)
    message = Column(Text)
    items_new = Column(Integer, default=0, nullable=False)
    articles_made = Column(Integer, default=0, nullable=False)
    duration_s = Column(Float)
    started_at = Column(DateTime, default=utcnow, nullable=False)
    finished_at = Column(DateTime)

    feed = relationship("Feed")


class OpdsAccess(Base):
    """One HTTP request to an /opds endpoint.

    Kept so you can answer "did the ereader actually connect last night?"
    across restarts. Byte counts come from the ASGI layer, so they reflect what
    really left the socket -- a reader that gave up halfway shows a short
    transfer here rather than looking like a clean download.
    """

    __tablename__ = "opds_access"
    __table_args__ = (Index("ix_access_ts", "ts"),)

    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, default=utcnow, nullable=False)
    method = Column(String(8), nullable=False, default="GET")
    path = Column(String(512), nullable=False, default="")
    query = Column(String(512))
    status = Column(Integer, default=0, nullable=False)
    client_ip = Column(String(64))
    user_agent = Column(String(255))
    range_header = Column(String(128))
    bytes_sent = Column(Integer, default=0, nullable=False)
    duration_ms = Column(Integer, default=0, nullable=False)
    # Set when the path addressed a specific edition, so the UI can say how
    # much of that book the request actually moved.
    edition_id = Column(Integer)
    kind = Column(String(16), default="other", nullable=False)  # catalog/cover/download
    disconnected = Column(Boolean, default=False, nullable=False)


class Setting(Base):
    """User-editable key/value config, all of it set from the web UI."""

    __tablename__ = "settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, default="")
