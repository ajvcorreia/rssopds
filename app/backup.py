"""Backup and restore of the whole data directory.

A backup is a single .tar.gz containing a consistent snapshot of the database
plus every EPUB, cached image and cover -- everything under `config.data_dir`
except the WAL/SHM sidecar files, which a checkpointed snapshot has no need
for. It exists for two purposes that turn out to be the same file: guarding
against data loss, and moving the whole installation to a new machine (feeds,
categories, articles, read state, cached art -- all of it, not just config).

What it deliberately does NOT include: `.env`. Credentials are server-local by
design and are never written into the database, so a backup carries no web or
OPDS password. It DOES carry whatever is in the database, which includes any
configured fediverse (Threads) access tokens -- handle a backup file the way
you would handle those tokens themselves.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import sqlite3
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from .config import config

log = logging.getLogger(__name__)

# Directories copied verbatim; the database gets a consistent snapshot instead
# of a raw file copy (see `_snapshot_database`).
DATA_SUBDIRS = ("epub", "images", "covers")

REQUIRED_TABLES = {"feeds", "categories", "articles", "editions", "settings"}

PRE_RESTORE_PREFIX = "_pre_restore_"
KEEP_PRE_RESTORE_COPIES = 2


class BackupError(RuntimeError):
    """A backup or restore step failed in a way the caller should show."""


def _snapshot_database(dest_path: Path) -> None:
    """Write a consistent copy of the live database to `dest_path`.

    Uses SQLite's own backup API rather than copying the file: the app runs
    in WAL mode and is written to by background jobs concurrently, so a plain
    file copy could grab a torn write or miss data still sitting in the WAL.
    The backup API is exactly what it is for -- a safe, consistent snapshot
    of a database that is live and being written to at the same time.
    """
    source = sqlite3.connect(f"file:{config.db_path}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def create_backup_archive() -> Path:
    """Build a backup .tar.gz in a temp file and return its path.

    Caller is responsible for deleting the returned file once it has been
    served or otherwise finished with.
    """
    fd, tmp_name = tempfile.mkstemp(prefix="rssopds-backup-", suffix=".tar.gz")
    os.close(fd)
    out_path = Path(tmp_name)

    with tempfile.TemporaryDirectory(prefix="rssopds-backup-stage-") as stage_str:
        stage = Path(stage_str)
        _snapshot_database(stage / config.db_path.name)

        for name in DATA_SUBDIRS:
            source_dir = config.data_dir / name
            if source_dir.is_dir():
                shutil.copytree(source_dir, stage / name)

        with tarfile.open(out_path, "w:gz") as tar:
            for item in sorted(stage.iterdir()):
                tar.add(item, arcname=item.name)

    log.info("built backup archive %s (%d bytes)",
             out_path.name, out_path.stat().st_size)
    return out_path


def backup_filename() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"rssopds-backup-{stamp}.tar.gz"


# --- restore ---------------------------------------------------------------

def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract, refusing anything that would land outside `dest`.

    Belt and suspenders: `filter="data"` (Python's own tarslip guard) plus an
    explicit resolved-path check, since restore accepts a file upload and a
    crafted archive escaping the intended directory would be an arbitrary
    file write.
    """
    dest_resolved = dest.resolve()
    for member in tar.getmembers():
        if member.issym() or member.islnk():
            raise BackupError(f"archive contains a link ({member.name}), refusing it")
        target = (dest / member.name).resolve()
        if target != dest_resolved and dest_resolved not in target.parents:
            raise BackupError(f"archive entry escapes the target directory: "
                              f"{member.name}")
    try:
        tar.extractall(dest, filter="data")
    except TypeError:
        # Python < 3.12 without the backport: the manual check above already
        # ran, so this is still safe, just without the extra stdlib guard.
        tar.extractall(dest)


def _validate_staged_database(stage: Path) -> None:
    db_file = stage / config.db_path.name
    if not db_file.is_file():
        raise BackupError(f"archive has no {config.db_path.name} -- "
                          "this does not look like an RSSOPDS backup")
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise BackupError(f"{config.db_path.name} in the archive is not a "
                          f"readable SQLite database: {exc}") from None
    missing = REQUIRED_TABLES - tables
    if missing:
        raise BackupError("archive's database is missing expected tables: "
                          + ", ".join(sorted(missing)))


def validate_backup_upload(fileobj: BinaryIO) -> Path:
    """Extract an uploaded backup to a staging directory and validate it.

    Returns the staging directory on success; raises BackupError otherwise.
    Nothing under `config.data_dir` is touched by this step -- a bad upload
    is rejected before anything about the current installation changes.
    """
    stage = Path(tempfile.mkdtemp(prefix="rssopds-restore-stage-"))
    try:
        with tarfile.open(fileobj=fileobj, mode="r:*") as tar:
            _safe_extract(tar, stage)
    except tarfile.TarError as exc:
        shutil.rmtree(stage, ignore_errors=True)
        raise BackupError(f"could not read the uploaded file as a "
                          f"backup archive: {exc}") from None
    except BackupError:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    try:
        _validate_staged_database(stage)
    except BackupError:
        shutil.rmtree(stage, ignore_errors=True)
        raise

    return stage


def _existing_pre_restore_dirs() -> list[Path]:
    return [p for p in config.data_dir.glob(f"{PRE_RESTORE_PREFIX}*") if p.is_dir()]


def _next_pre_restore_seq() -> int:
    best = 0
    for p in _existing_pre_restore_dirs():
        try:
            seq = int(p.name[len(PRE_RESTORE_PREFIX):].split("_", 1)[0])
        except ValueError:
            continue
        best = max(best, seq)
    return best + 1


def _prune_pre_restore_copies() -> None:
    # The leading sequence number sorts lexically the same as numerically
    # (zero-padded), so this is chronological order regardless of what a
    # same-second timestamp or mkdtemp's random suffix look like. A slice
    # past the end of a short list is just empty, so no length check needed.
    copies = sorted(_existing_pre_restore_dirs(), key=lambda p: p.name)
    for stale in copies[:-KEEP_PRE_RESTORE_COPIES]:
        shutil.rmtree(stale, ignore_errors=True)
        log.info("removed old pre-restore safety copy %s", stale.name)


def apply_restore(stage: Path) -> None:
    """Swap the validated staging directory into place as the live data.

    The current database and files are moved aside into a timestamped
    `_pre_restore_<time>` folder rather than deleted, so a restore that turns
    out to be a mistake can still be recovered by hand. Only the two most
    recent such folders are kept.

    Deliberately does not touch the live SQLAlchemy engine or scheduler --
    the caller is expected to end the process immediately afterwards so the
    next start picks up the swapped files cleanly. Making that restart happen
    is the caller's job (see the /settings/restore route), not this
    function's, so tests can call this directly without killing themselves.
    """
    config.ensure_dirs()

    # Release any pooled connections before touching a single file. A rename
    # does not invalidate an already-open file descriptor, so anything still
    # held open here would keep reading and writing the file being moved
    # aside -- on Linux that is a silent correctness problem (a reused
    # connection would go on writing to what is now the pre-restore copy);
    # on Windows the OS refuses to rename or delete an open file outright.
    from .db import _engine
    _engine.dispose()

    # mkdtemp, not a hand-built timestamp name: two restores landing in the
    # same second -- plausible in a script, and exactly the kind of thing a
    # human doesn't expect to hit but a retry loop finds immediately -- would
    # otherwise collide on the folder name and crash the second restore.
    #
    # The leading sequence number, not the timestamp, is what pruning later
    # sorts by. Two restores in the same second get identical timestamps, so
    # the only thing distinguishing their folder names would be mkdtemp's
    # random suffix -- and alphabetical order on a random suffix has no
    # relationship to which one was actually created first. Sorting by that
    # let the kept-copy count drift upward across repeated restores instead
    # of staying capped, since which folders counted as "most recent" could
    # reshuffle on every prune.
    seq = _next_pre_restore_seq()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    pre_restore = Path(tempfile.mkdtemp(
        dir=str(config.data_dir), prefix=f"{PRE_RESTORE_PREFIX}{seq:06d}_{stamp}-"))

    # Move the current database (plus any WAL/SHM sidecars) and asset
    # directories aside first, then move the restored ones into place. Two
    # passes rather than one so a failure partway through never leaves a
    # mix of half-old, half-new files silently in place.
    db_name = config.db_path.name
    for suffix in ("", "-wal", "-shm"):
        current = config.data_dir / f"{db_name}{suffix}"
        if current.exists():
            shutil.move(str(current), str(pre_restore / current.name))
    for name in DATA_SUBDIRS:
        current = config.data_dir / name
        if current.is_dir():
            shutil.move(str(current), str(pre_restore / name))

    shutil.move(str(stage / db_name), str(config.data_dir / db_name))
    for name in DATA_SUBDIRS:
        staged = stage / name
        if staged.is_dir():
            shutil.move(str(staged), str(config.data_dir / name))
    shutil.rmtree(stage, ignore_errors=True)

    _prune_pre_restore_copies()
    log.info("restore applied; previous data moved to %s", pre_restore.name)


def schedule_restart(delay_s: float = 1.5) -> None:
    """End this process shortly after returning, so Docker's restart policy
    brings it back up against the freshly-restored files.

    Restoring while staying in the same process would mean the running
    SQLAlchemy engine, connection pool and scheduler keep referencing the
    database and files that used to be there. Exiting and letting the
    container restart is simpler and safer than trying to hot-swap all of
    that live, and this app already restarts cleanly on every deploy.
    """
    def _die():
        time.sleep(delay_s)
        log.info("restarting to apply the restored data")
        os._exit(0)

    threading.Thread(target=_die, daemon=True).start()
