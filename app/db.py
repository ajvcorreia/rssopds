"""Engine, session factory and first-run bootstrap."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import config
from .models import Base

log = logging.getLogger(__name__)

_engine = create_engine(
    f"sqlite:///{config.db_path}",
    future=True,
    # The scheduler runs jobs on worker threads while requests are served on
    # others; SQLite connections are per-thread from the pool, so this is safe.
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(_engine, "connect")
def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    # WAL keeps the poller writing while the ereader streams an EPUB.
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False,
                            future=True)


def _add_missing_columns() -> None:
    """Bring an existing SQLite file up to the current model.

    create_all() adds new tables but never new columns, so a column added to a
    model would blow up against a database created before it. Full Alembic
    migrations are overkill for a single-file app; adding nullable/defaulted
    columns covers every schema change this app has needed.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(_engine)
    existing_tables = set(inspector.get_table_names())

    with _engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all will make it
            have = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in have:
                    continue
                ddl = column.type.compile(_engine.dialect)
                default = column.default.arg if column.default is not None else None
                if callable(default):
                    default = None
                clause = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}'
                if default is not None:
                    literal = (f"'{default}'" if isinstance(default, str)
                               else int(default) if isinstance(default, bool)
                               else default)
                    clause += f" DEFAULT {literal}"
                log.info("schema: %s", clause)
                conn.execute(text(clause))


def _backfill_edition_numbers(session) -> None:
    """One-time: give pre-existing editions sequential issue numbers.

    Rows that predate the `number` column all carry the same default, so
    without this the next edition would be numbered 2 no matter how many came
    before it.

    Guarded by a marker row rather than re-checked on every boot. The
    original heuristic ("are all numbers already distinct?") stopped being
    safe once build_category started deliberately reusing a number for an
    edition that was superseded without ever being downloaded -- two editions
    legitimately sharing a number is normal now, and re-running that check
    would read a healthy database as "not yet migrated" and renumber
    everything sequentially again, silently undoing the reuse on every
    restart.
    """
    from sqlalchemy import select as sa_select

    from .models import Edition, Setting

    marker_key = "_schema_edition_numbers_backfilled"
    if session.get(Setting, marker_key) is not None:
        return

    category_ids = [
        c for (c,) in session.execute(sa_select(Edition.category_id).distinct())
    ]
    for category_id in category_ids:
        rows = list(session.execute(
            sa_select(Edition).where(Edition.category_id == category_id)
            .order_by(Edition.id)
        ).scalars())
        if len({r.number for r in rows}) == len(rows):
            continue  # already numbered distinctly
        for index, edition in enumerate(rows, start=1):
            edition.number = index
        log.info("renumbered %d editions in category %s", len(rows), category_id)

    session.add(Setting(key=marker_key, value="1"))


LEGACY_DB_NAME = "rssospd.db"  # the project was briefly misspelled


def adopt_legacy_database() -> None:
    """Rename a database left over from the old project spelling.

    Without this the app would find no file, create an empty one, and quietly
    lose every feed, article and edition -- with nothing in the logs to say
    why. Runs before the engine touches the file.
    """
    new = config.db_path
    old = new.with_name(LEGACY_DB_NAME)
    if new.exists() or not old.exists():
        return
    for suffix in ("", "-wal", "-shm"):
        source = old.with_name(old.name + suffix)
        if source.exists():
            source.rename(new.with_name(new.name + suffix))
    log.info("adopted database from %s as %s", old.name, new.name)


def init_db() -> None:
    config.ensure_dirs()
    adopt_legacy_database()
    Base.metadata.create_all(_engine)
    _add_missing_columns()
    from .settings_store import seed_defaults
    with session_scope() as s:
        seed_defaults(s)
        _backfill_edition_numbers(s)


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
