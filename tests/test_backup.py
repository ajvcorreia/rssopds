"""Backup and restore: archive contents, path-traversal safety, and a real
round trip through the actual restore swap (never through schedule_restart,
which ends the process -- that half is checked by monkeypatching it out)."""
import io
import sqlite3
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app import backup  # noqa: E402
from app.config import config  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Article, ArticleState, Category, Feed, SourceKind  # noqa: E402

# The suite must not depend on ambient environment. A deployed container has
# real web-auth credentials configured, which would 401 every TestClient
# call in section [7] -- the actual value doesn't matter here, unlike
# test_pipeline.py, so it is simply cleared rather than exercised.
config.web_user = config.web_password = ""
config.opds_user = config.opds_password = ""
config.opds_public = False

FAILS = []


def check(label, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: got {got!r}, want {want!r}")
    if not ok:
        FAILS.append(label)


def truthy(label, got):
    ok = bool(got)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}")
    if not ok:
        FAILS.append(label)


def expect_raises(label, fn, exc_type=backup.BackupError):
    try:
        fn()
    except exc_type as exc:
        print(f"  PASS  {label}: raised {exc_type.__name__} ({exc})")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  {label}: raised {type(exc).__name__}, not {exc_type.__name__}")
        FAILS.append(label)
    else:
        print(f"  FAIL  {label}: did not raise")
        FAILS.append(label)


init_db()

print("\n[1] a fresh backup contains a valid, matching database")
with session_scope() as s:
    cat = Category(name="Backup Test", slug="backup-test")
    s.add(cat)
    s.flush()
    feed = Feed(title="Backup Feed", kind=SourceKind.rss, url="http://x",
               category_id=cat.id, fedi_token="super-secret-token")
    s.add(feed)
    s.flush()
    s.add(Article(feed_id=feed.id, category_id=cat.id, title="Alpha article",
                  body_html="<p>Alpha body.</p>", word_count=2,
                  state=ArticleState.ready))
    s.flush()

archive_path = backup.create_backup_archive()
truthy("archive file exists", archive_path.exists())
truthy("archive has real size", archive_path.stat().st_size > 500)

with tarfile.open(archive_path) as tar:
    names = tar.getnames()
    check("db file present at archive root", config.db_path.name in names, True)
    truthy("epub/images/covers dirs present (even if empty)",
           any(n.startswith(("epub", "images", "covers")) for n in names)
           or all(not (config.data_dir / d).iterdir() for d in backup.DATA_SUBDIRS
                  if (config.data_dir / d).is_dir()))

    db_bytes = tar.extractfile(config.db_path.name).read()
    tmp_db = config.data_dir / "_extracted_check.db"
    tmp_db.write_bytes(db_bytes)
    conn = sqlite3.connect(tmp_db)
    title = conn.execute("SELECT title FROM articles WHERE title LIKE 'Alpha%'").fetchone()
    token = conn.execute("SELECT fedi_token FROM feeds WHERE title='Backup Feed'").fetchone()
    conn.close()
    tmp_db.unlink()
    check("the article in the snapshot matches the live database",
          title, ("Alpha article",))
    truthy("a fedi_token IS included (documented, not a bug)", token[0])
    check("token value carried through exactly", token[0], "super-secret-token")

print("\n[2] the .env-equivalent (credentials) is never in the archive")
with tarfile.open(archive_path) as tar:
    truthy("no .env-named entry in the archive",
           not any("env" == Path(n).name.lstrip(".") for n in tar.getnames()))

print("\n[3] path traversal and link entries are refused before touching disk")


def _malicious_tar(build) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        build(tar)
    buf.seek(0)
    return buf


def _add_bytes(tar, name, data=b"x"):
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


evil_traversal = _malicious_tar(
    lambda tar: _add_bytes(tar, "../../../etc/evil.txt"))
expect_raises("../ escape is refused",
             lambda: backup.validate_backup_upload(evil_traversal))

evil_absolute = _malicious_tar(
    lambda tar: _add_bytes(tar, "/etc/evil.txt"))
expect_raises("absolute path is refused",
             lambda: backup.validate_backup_upload(evil_absolute))


def _add_symlink(tar):
    info = tarfile.TarInfo(name="innocent.txt")
    info.type = tarfile.SYMTYPE
    info.linkname = "/etc/passwd"
    tar.addfile(info)


evil_symlink = _malicious_tar(_add_symlink)
expect_raises("a symlink entry is refused",
             lambda: backup.validate_backup_upload(evil_symlink))

print("\n[4] a garbage or wrong-shaped upload is rejected, nothing touched")
not_a_tar = io.BytesIO(b"this is not a tar file at all")
expect_raises("non-tar upload rejected", lambda: backup.validate_backup_upload(not_a_tar))

no_db = _malicious_tar(lambda tar: _add_bytes(tar, "epub/readme.txt"))
expect_raises("archive with no database file rejected",
             lambda: backup.validate_backup_upload(no_db))

fake_db = _malicious_tar(
    lambda tar: _add_bytes(tar, config.db_path.name, b"not a real sqlite file"))
expect_raises("archive whose db is not real sqlite rejected",
             lambda: backup.validate_backup_upload(fake_db))


def _wrong_schema_tar() -> io.BytesIO:
    tmp = config.data_dir / "_wrong_schema.db"
    conn = sqlite3.connect(tmp)
    conn.execute("CREATE TABLE something_else (id INTEGER)")
    conn.commit()
    conn.close()
    data = tmp.read_bytes()
    tmp.unlink()
    return _malicious_tar(lambda tar: _add_bytes(tar, config.db_path.name, data))


expect_raises("a real sqlite file with the wrong tables is rejected",
             lambda: backup.validate_backup_upload(_wrong_schema_tar()))

with session_scope() as s:
    still_there = s.query(Article).filter_by(title="Alpha article").one_or_none()
truthy("none of the rejected uploads touched the live database",
       still_there is not None)

print("\n[5] a full backup -> restore round trip")
with session_scope() as s:
    before_articles = sorted(a.title for a in s.query(Article).all())
    before_feed_count = s.query(Feed).count()

good_archive = backup.create_backup_archive()
with open(good_archive, "rb") as fh:
    stage = backup.validate_backup_upload(fh)
good_archive.unlink()

# Mutate the "live" data so the restore has something real to undo: a new
# article that must disappear, and the old one that must come back.
with session_scope() as s:
    s.query(Article).filter_by(title="Alpha article").delete()
    cat2 = s.query(Category).filter_by(slug="backup-test").one()
    s.add(Article(feed_id=s.query(Feed).filter_by(title="Backup Feed").one().id,
                  category_id=cat2.id, title="Should vanish on restore",
                  body_html="<p>x</p>", word_count=1, state=ArticleState.ready))

with session_scope() as s:
    titles_before_restore = sorted(a.title for a in s.query(Article).all())
truthy("mutation actually took effect before restoring",
       "Should vanish on restore" in titles_before_restore
       and "Alpha article" not in titles_before_restore)

backup.apply_restore(stage)

# apply_restore only swaps files on disk; the running app's engine/session
# factory still needs a fresh connection to see them. A brand-new session
# reads from the swapped-in file same as a restarted process would.
with session_scope() as s:
    after_titles = sorted(a.title for a in s.query(Article).all())
check("the pre-mutation article is back", "Alpha article" in after_titles, True)
truthy("the post-backup addition is gone",
       "Should vanish on restore" not in after_titles)

pre_restore_dirs = [p for p in config.data_dir.glob(f"{backup.PRE_RESTORE_PREFIX}*")
                    if p.is_dir()]
truthy("a pre-restore safety copy was made", pre_restore_dirs)
# sqlite3.Connection's context manager only wraps the transaction (commit or
# rollback) -- it does not close the connection. Closing explicitly here
# matters more than usual: on Windows a directory containing an open file
# handle cannot be deleted, so a leaked connection into a pre-restore copy
# would make every later prune of it silently fail via ignore_errors=True.
conn = sqlite3.connect(pre_restore_dirs[-1] / config.db_path.name)
try:
    saved = [r[0] for r in conn.execute("SELECT title FROM articles")]
finally:
    conn.close()
truthy("the safety copy holds the data from BEFORE the restore, not after",
       "Should vanish on restore" in saved)

print("\n[6] only the most recent pre-restore copies are kept")
for _ in range(5):
    arc = backup.create_backup_archive()
    with open(arc, "rb") as fh:
        st = backup.validate_backup_upload(fh)
    arc.unlink()
    backup.apply_restore(st)
kept = [p for p in config.data_dir.glob(f"{backup.PRE_RESTORE_PREFIX}*") if p.is_dir()]
check(f"at most {backup.KEEP_PRE_RESTORE_COPIES} safety copies are kept",
      len(kept) <= backup.KEEP_PRE_RESTORE_COPIES, True)

print("\n[7] the web routes, without ever actually restarting the process")
_restart_calls = []
_real_schedule_restart = backup.schedule_restart
backup.schedule_restart = lambda *a, **k: _restart_calls.append(True)

client = TestClient(app)
try:
    r = client.get("/settings/backup")
    check("download responds 200", r.status_code, 200)
    check("download content type", r.headers.get("content-type"), "application/gzip")
    truthy("download has a filename",
           "rssopds-backup-" in r.headers.get("content-disposition", ""))
    with tarfile.open(fileobj=io.BytesIO(r.content)) as tar:
        truthy("downloaded archive contains the database",
               config.db_path.name in tar.getnames())

    bad_upload = ("junk.tar.gz", io.BytesIO(b"not a backup"), "application/gzip")
    r = client.post("/settings/restore", files={"backup_file": bad_upload},
                    follow_redirects=False)
    check("rejected upload redirects with an error", r.status_code, 303)
    truthy("error message present in the redirect",
           "err=" in r.headers.get("location", ""))
    check("restart was NOT scheduled for a rejected upload", len(_restart_calls), 0)

    real_archive = backup.create_backup_archive()
    with open(real_archive, "rb") as fh:
        good_upload = ("good.tar.gz", io.BytesIO(fh.read()), "application/gzip")
    real_archive.unlink()
    r = client.post("/settings/restore", files={"backup_file": good_upload},
                    follow_redirects=False)
    check("a valid restore redirects to settings", r.status_code, 303)
    check("restart WAS scheduled for a valid restore", len(_restart_calls), 1)
finally:
    backup.schedule_restart = _real_schedule_restart
    from apscheduler.schedulers.base import STATE_PAUSED

    from app import scheduler as scheduler_mod
    if scheduler_mod.scheduler.state == STATE_PAUSED:
        scheduler_mod.scheduler.resume()

print("\n" + "=" * 60)
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("ALL CHECKS PASSED")
