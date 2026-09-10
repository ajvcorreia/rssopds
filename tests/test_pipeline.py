"""End-to-end smoke test: thread merge -> epub -> OPDS -> ranged download."""
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import config  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    Article, ArticleState, Category, Delivery, Edition, EditionArticle,
    EditionState, Feed, FeedItem, JobRun, SourceKind, ThreadMode,
)
from app.pipeline import assemble, editions, process  # noqa: E402
from app.settings_store import put  # noqa: E402

FAILS = []

# The suite must not depend on ambient environment. A deployed container has
# real credentials in its environment, which would 401 every TestClient call;
# section [24] switches auth on deliberately and restores it afterwards.
from app.config import config as _config  # noqa: E402

_config.web_user = _config.web_password = ""
_config.opds_user = _config.opds_password = ""
_config.opds_public = False


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


init_db()

NOW = datetime.now(timezone.utc)

print("\n[setup] category + fediverse feed in merge_self mode")
with session_scope() as s:
    put(s, "thread_grace_minutes", 180)
    put(s, "build_min_articles", 1)

    cat = Category(name="Threads", slug="threads")
    s.add(cat)
    s.flush()
    feed = Feed(title="Threads bot", kind=SourceKind.fediverse,
                url="https://mastodon.example", fedi_token="x",
                category_id=cat.id, thread_mode=ThreadMode.merge_self,
                clean_with_ai=False, extract_fulltext=False,
                include_images=False)
    s.add(feed)
    s.flush()
    cat_id, feed_id = cat.id, feed.id

    # A three-post self-thread by account "77", plus a reply to someone else.
    parts = [
        ("uri:a", None, None, "<p>Starting a thread about e-readers. 1/3</p>"),
        ("uri:b", "uri:a", "77", "<p>The screen refresh is the hard part. 2/3</p>"),
        ("uri:c", "uri:b", "77", "<p>Anyway, that is why I built this. 3/3</p>"),
    ]
    for i, (guid, parent, parent_acct, body) in enumerate(parts):
        s.add(FeedItem(feed_id=feed_id, guid=guid, author="Bot", author_key="77",
                       content_html=body, url=f"https://threads.net/{guid}",
                       published_at=NOW - timedelta(minutes=30 - i),
                       reply_to_guid=parent, reply_to_author_key=parent_acct))
    # A reply to a different account -- should be skipped, not published.
    s.add(FeedItem(feed_id=feed_id, guid="uri:d", author="Bot", author_key="77",
                   content_html="<p>totally agree!</p>",
                   published_at=NOW - timedelta(minutes=5),
                   reply_to_guid="uri:someone-else", reply_to_author_key="99"))

print("\n[1] assemble merges the self-thread")
with session_scope() as s:
    made = assemble.assemble_all(s)
check("articles created", made, 1)

with session_scope() as s:
    art = s.query(Article).one()
    check("part_count", art.part_count, 3)
    check("thread_key", art.thread_key, "uri:a")
    truthy("title derived from first post", art.title)
    print(f"        title = {art.title!r}")
    skipped = s.query(FeedItem).filter(FeedItem.skipped.is_(True)).all()
    check("reply-to-other skipped", len(skipped), 1)
    check("skip reason", skipped[0].guid, "uri:d")

print("\n[2] the thread is held open, so nothing is processed yet")
with session_scope() as s:
    due = process.due_articles(s)
check("articles due while held", len(due), 0)

print("\n[3] a late 4th part joins the held thread")
with session_scope() as s:
    s.add(FeedItem(feed_id=feed_id, guid="uri:e", author="Bot", author_key="77",
                   content_html="<p>One more thing I forgot. 4/4</p>",
                   published_at=NOW - timedelta(minutes=2),
                   reply_to_guid="uri:c", reply_to_author_key="77"))
with session_scope() as s:
    assemble.assemble_all(s)
with session_scope() as s:
    check("articles still 1 (no orphan)", s.query(Article).count(), 1)
    check("part_count grew", s.query(Article).one().part_count, 4)

print("\n[4] hold expires -> processed, thread markers stripped")
with session_scope() as s:
    put(s, "thread_grace_minutes", 0)
    s.query(Article).update({Article.thread_open_until: None})
with session_scope() as s:
    done = process.process_batch(s)
check("processed", done, 1)
with session_scope() as s:
    art = s.query(Article).one()
    check("state", art.state, ArticleState.ready)
    truthy("has body", art.word_count > 0)
    truthy("'1/3' marker removed", "1/3" not in art.body_html)
    truthy("'4/4' marker removed", "4/4" not in art.body_html)
    truthy("all four parts present",
           all(w in art.body_html for w in
               ("e-readers", "refresh", "built this", "forgot")))
    print(f"        body = {art.body_html!r}")

print("\n[5] build an edition")
with session_scope() as s:
    built = editions.build_all(s)
check("editions built", built, 1)
with session_scope() as s:
    ed = s.query(Edition).one()
    check("edition state", ed.state, EditionState.available)
    check("article count", ed.article_count, 1)
    truthy("epub on disk", (config.epub_dir / ed.epub_file).exists())
    truthy("epub non-trivial size", ed.size_bytes > 1000)
    ed_id, ed_size = ed.id, ed.size_bytes
    print(f"        {ed.epub_file} ({ed_size} bytes)")

client = TestClient(app)

print("\n[6] OPDS catalogue lists the category")
r = client.get("/opds")
check("status", r.status_code, 200)
truthy("category present", "Threads" in r.text)
truthy("acquisition link", f"/opds/edition/{ed_id}/" in r.text)
truthy("cover link", f"/opds/cover/{cat_id}.jpg" in r.text)

print("\n[7] cover image renders")
r = client.get(f"/opds/cover/{cat_id}.jpg")
check("cover status", r.status_code, 200)
truthy("is a jpeg", r.content[:2] == b"\xff\xd8")

print("\n[8] a partial ranged download must NOT mark anything read")
half = ed_size // 2
r = client.get(f"/opds/edition/{ed_id}.epub",
               headers={"Range": f"bytes=0-{half - 1}"})
check("partial status", r.status_code, 206)
check("partial length", len(r.content), half)
with session_scope() as s:
    check("edition still available", s.get(Edition, ed_id).state,
          EditionState.available)
    check("article still published", s.query(Article).one().state,
          ArticleState.published)

print("\n[9] the rest of the range completes it")
r = client.get(f"/opds/edition/{ed_id}.epub",
               headers={"Range": f"bytes={half}-"})
check("remainder status", r.status_code, 206)
with session_scope() as s:
    d = s.get(Edition, ed_id).deliveries[0]
    check("coverage complete", d.complete, True)
    check("bytes covered", d.bytes_sent, ed_size)

print("\n[10] confirm delay respected, then articles marked read")
with session_scope() as s:
    put(s, "delivery_confirm_delay_s", 3600)
with session_scope() as s:
    check("not confirmed while delay pending",
          editions.confirm_due_deliveries(s), 0)
with session_scope() as s:
    put(s, "delivery_confirm_delay_s", 0)
with session_scope() as s:
    check("confirmed", editions.confirm_due_deliveries(s), 1)
with session_scope() as s:
    check("edition delivered", s.get(Edition, ed_id).state,
          EditionState.delivered)
    check("article read", s.query(Article).one().state, ArticleState.delivered)

print("\n[11] empty category disappears from the catalogue")
r = client.get("/opds")
truthy("category gone", "urn:opds:edition" not in r.text)
truthy("still valid atom", r.text.startswith("<?xml"))
truthy("shows nothing-new entry", "Nothing new" in r.text)

print("\n[12] web UI pages render")
for path in ("/", "/feeds", "/feeds/new", "/threads", "/categories",
             "/articles", "/editions", "/jobs", "/settings", "/healthz"):
    r = client.get(path)
    ok = r.status_code == 200
    print(f"  {'PASS' if ok else 'FAIL'}  GET {path} -> {r.status_code}")
    if not ok:
        FAILS.append(f"GET {path}")
        print(r.text[:600])

r = client.get(f"/articles/1")
check("article detail", r.status_code, 200)

print("\n[13] undo delivery puts it back")
with session_scope() as s:
    editions.undo_delivery(s, s.get(Edition, ed_id))
with session_scope() as s:
    check("edition available again", s.get(Edition, ed_id).state,
          EditionState.available)
    check("article unread again", s.query(Article).one().state,
          ArticleState.published)

print("\n[14] a manual 'mark unread' survives a late delivery confirmation")
with session_scope() as s:
    put(s, "delivery_confirm_delay_s", 0)
    # Edition is available again with a complete delivery still on record;
    # the user then pulls one article back out of it.
    d = s.get(Edition, ed_id).deliveries[0]
    d.complete, d.bytes_sent = True, ed_size
    d.completed_at = datetime.now(timezone.utc) - timedelta(hours=1)
    s.query(Article).update({Article.state: ArticleState.ready})
with session_scope() as s:
    editions.confirm_due_deliveries(s)
with session_scope() as s:
    check("manually-unread article stays unread",
          s.query(Article).one().state, ArticleState.ready)

print("\n[15] the OPDS access log records what clients did")
from app.models import OpdsAccess  # noqa: E402
from app.opds import access_log  # noqa: E402

with session_scope() as s:
    rows = access_log.recent(s, limit=100)
    kinds = {r.kind for r in rows}
    truthy("catalog requests logged", "catalog" in kinds)
    truthy("cover requests logged", "cover" in kinds)
    truthy("download requests logged", "download" in kinds)

    downloads = [r for r in rows if r.kind == "download"]
    # Section [8] fetched half the file, [9] fetched the rest.
    partial = [r for r in downloads if 0 < r.bytes_sent < ed_size]
    truthy("a partial transfer is visible as short", partial)
    check("partial request carried a Range header",
          bool(partial and partial[0].range_header), True)
    truthy("byte counts are real, not content-length guesses",
           all(r.bytes_sent > 0 for r in downloads))
    check("edition id attributed", downloads[0].edition_id, ed_id)

    cat = next(r for r in rows if r.kind == "catalog")
    check("status recorded", cat.status, 200)
    truthy("client ip recorded", cat.client_ip)

    people = access_log.clients(s)
    truthy("client summary produced", people)
    check("summary counts downloads",
          people[0]["downloads"] >= 2, True)

# A 404 must be logged too -- that is how you spot a reader asking for a
# pruned edition.
r = client.get("/opds/edition/9999.epub")
check("missing edition 404s", r.status_code, 404)
with session_scope() as s:
    errs = access_log.recent(s, limit=20, kind="errors")
    truthy("404 recorded in the error view", any(e.status == 404 for e in errs))

print("\n[16] the log page and its live-refresh endpoint")
r = client.get("/opds-log")
check("log page renders", r.status_code, 200)
truthy("shows a client row", "Clients seen" in r.text)
r = client.get("/opds-log/data?since_id=0")
check("data endpoint", r.status_code, 200)
payload = r.json()
truthy("returns entries", payload["entries"])
truthy("returns clients", payload["clients"])
truthy("download rows carry a percentage",
       any(e["pct"] is not None for e in payload["entries"]))
newest = payload["entries"][0]["id"]
r2 = client.get(f"/opds-log/data?since_id={newest}")
check("since_id filters out what we already have",
      r2.json()["entries"], [])
truthy("the log page does not log itself",
       not any("/opds-log" in e["path"] for e in payload["entries"]))
client.get("/opds")
r3 = client.get(f"/opds-log/data?since_id={newest}")
check("a genuine new request does show up",
      [e["path"] for e in r3.json()["entries"]], ["/opds"])

print("\n[17] two editions built the same day must not collide on device")
# Reproduces the real report: an ereader names the saved file from the OPDS
# title, so "Technology - 08 Sep 2026" twice in one day overwrote itself.
with session_scope() as s:
    put(s, "delivery_confirm_delay_s", 3600)
    s.query(Article).update({Article.state: ArticleState.ready})
with session_scope() as s:
    editions.build_all(s)

with session_scope() as s:
    built = s.query(Edition).order_by(Edition.id).all()
    titles = [e.title for e in built]
    files = [e.epub_file for e in built if e.epub_file]
    numbers = [e.number for e in built]
    check("a second edition was built today", len(built) >= 2, True)
    check("titles are distinct", len(set(titles)), len(titles))
    check("filenames on disk are distinct", len(set(files)), len(files))
    check("issue numbers increment", numbers, sorted(set(numbers)))
    print("        titles:", titles)

r = client.get("/opds")
hrefs = re.findall(r'href="([^"]*\.epub)"', r.text)
truthy("catalog advertises a named .epub URL",
       hrefs and not hrefs[0].rstrip("0123456789").endswith("/edition/"))
print("        acquisition href:", hrefs[0] if hrefs else None)

# Whichever of the three a reader uses, it must differ between editions.
name_url = hrefs[0].rsplit("/", 1)[-1]
resp = client.get(hrefs[0].replace("http://testserver", ""))
check("named URL downloads", resp.status_code, 200)
disp = resp.headers.get("content-disposition", "")
truthy("content-disposition carries the unique name",
       name_url.rsplit(".epub", 1)[0] in disp)

with session_scope() as s:
    latest = s.query(Edition).order_by(Edition.id.desc()).first()
    check("named URL and disk file agree", name_url, latest.epub_file)

check("old unnumbered URL still works",
      client.get(f"/opds/edition/{latest.id}.epub").status_code, 200)

with session_scope() as s:
    logged = access_log.recent(s, limit=10, kind="download")
    truthy("named URL is attributed to the right edition",
           any(e.edition_id == latest.id for e in logged))

print("\n[18] upgrading an old database")
from app.db import _backfill_edition_numbers  # noqa: E402
from app.models import Setting  # noqa: E402
from app.settings_store import seed_defaults  # noqa: E402

with session_scope() as s:
    # Simulate a database made before the column existed: every row default,
    # and the migration has genuinely never run against it (no marker yet).
    s.query(Edition).update({Edition.number: 1})
    marker = s.get(Setting, "_schema_edition_numbers_backfilled")
    if marker is not None:
        s.delete(marker)
with session_scope() as s:
    _backfill_edition_numbers(s)
with session_scope() as s:
    nums = [e.number for e in s.query(Edition).order_by(Edition.id).all()]
    check("old editions renumbered sequentially", nums, list(range(1, len(nums) + 1)))
    truthy("the migration marker is set afterwards",
           s.get(Setting, "_schema_edition_numbers_backfilled") is not None)

# It must not run a second time, even if numbers look "non-distinct" again --
# that is now a normal, intentional state (a rebuild reusing a number for an
# edition nobody has downloaded), not a sign of an unmigrated database.
with session_scope() as s:
    s.query(Edition).update({Edition.number: 1})
with session_scope() as s:
    _backfill_edition_numbers(s)
with session_scope() as s:
    nums_after = [e.number for e in s.query(Edition).order_by(Edition.id).all()]
    check("guarded: a marked database is never renumbered again",
          nums_after, [1] * len(nums_after))

with session_scope() as s:
    # A stored value that is just the old default gets moved forward...
    s.get(Setting, "edition_title_format").value = "{category} - {date}"
with session_scope() as s:
    seed_defaults(s)
with session_scope() as s:
    check("stale default title format upgraded",
          s.get(Setting, "edition_title_format").value,
          "No. {n} - {date}")

with session_scope() as s:
    # ...but a value the user actually chose is left alone.
    s.get(Setting, "edition_title_format").value = "My Paper {category} {time}"
with session_scope() as s:
    seed_defaults(s)
with session_scope() as s:
    check("user's own title format preserved",
          s.get(Setting, "edition_title_format").value,
          "My Paper {category} {time}")
    put(s, "edition_title_format", "No. {n} - {date}")

print("\n[19] a title format that would collide is rejected with a warning")
check("unique format accepted",
      editions.title_is_unique_per_edition("No. {n} - {date}"), True)
check("colliding format detected",
      editions.title_is_unique_per_edition("{category} - {date}"), False)
r = client.post("/settings", data={"edition_title_format": "{category} - {date}"},
                follow_redirects=False)
truthy("saving a colliding format warns the user",
       "overwrite" in r.headers.get("location", ""))
with session_scope() as s:
    put(s, "edition_title_format", "No. {n} - {date}")

print("\n[20] the application's own name reaches neither the catalogue nor the books")
import zipfile  # noqa: E402

r = client.get("/opds")
lowered = r.text.lower()
truthy("catalogue XML is free of the app name", "rssopds" not in lowered)
truthy("entry author is the category",
       "<name>Threads</name>" in r.text)
print("        entry author:", re.findall(r"<author><name>([^<]*)</name>", r.text))
print("        entry title :", re.findall(r"<title>([^<]*)</title>", r.text))

with session_scope() as s:
    latest = s.query(Edition).filter(Edition.epub_file.isnot(None)) \
        .order_by(Edition.id.desc()).first()
    path = config.epub_dir / latest.epub_file
    truthy("epub filename is free of the app name",
           "rssopds" not in latest.epub_file.lower())

with zipfile.ZipFile(path) as z:
    opf = next(n for n in z.namelist() if n.endswith(".opf"))
    meta = z.read(opf).decode("utf-8")
    truthy("epub metadata is free of the app name",
           "rssopds" not in meta.lower())
    truthy("epub creator is the category", "<dc:creator" in meta
           and "Threads" in meta)
    truthy("identifier is a plain uuid urn", "urn:uuid:" in meta)
    blob = b"".join(z.read(n) for n in z.namelist())
    truthy("no file inside the epub mentions the app name",
           b"rssopds" not in blob.lower())

print("\n[21] a configured publisher name overrides the category")
with session_scope() as s:
    put(s, "publisher_name", "The Daily Reader")
    s.query(Article).update({Article.state: ArticleState.ready})
with session_scope() as s:
    editions.build_all(s)
r = client.get("/opds")
truthy("publisher used as entry author",
       "<name>The Daily Reader</name>" in r.text)
with session_scope() as s:
    newest = s.query(Edition).filter(Edition.epub_file.isnot(None)) \
        .order_by(Edition.id.desc()).first()
    with zipfile.ZipFile(config.epub_dir / newest.epub_file) as z:
        opf = next(n for n in z.namelist() if n.endswith(".opf"))
        truthy("publisher written into the epub",
               "The Daily Reader" in z.read(opf).decode("utf-8"))
    put(s, "publisher_name", "")

print("\n[22] old branded settings are moved off the app name")
with session_scope() as s:
    s.get(Setting, "catalog_title").value = "RSSOPDS"
    s.get(Setting, "http_user_agent").value = "RSSOPDS/1.0 (+self-hosted feed reader)"
with session_scope() as s:
    seed_defaults(s)
with session_scope() as s:
    check("catalog title upgraded",
          s.get(Setting, "catalog_title").value, "Library")
    check("user agent upgraded",
          s.get(Setting, "http_user_agent").value, "FeedReader/1.0")

print("\n[23] confirming several deliveries at once must not crash the build")
# Regression: mark_delivered -> prune_delivered ran once per edition inside a
# single transaction. With autoflush off, the second pass re-selected rows
# whose epub_file had already been set to None in memory, and pruning them
# raised TypeError -- which killed the scheduled build job, silently stopping
# all automatic edition generation.
from app.models import Delivery  # noqa: E402

with session_scope() as s:
    put(s, "keep_delivered_editions", 0)   # force pruning of everything
    put(s, "delivery_confirm_delay_s", 0)
    cat2 = Category(name="Second", slug="second")
    s.add(cat2)
    s.flush()
    made = []
    for n in range(3):
        art = Article(feed_id=feed_id, category_id=cat2.id,
                      title=f"Filler {n}", body_html="<p>Body text here.</p>",
                      word_count=3, state=ArticleState.ready)
        s.add(art)
        made.append(art)
    s.flush()
    for n in range(3):
        ed = Edition(category_id=cat2.id, number=100 + n,
                     title=f"Bulk {n}", state=EditionState.available,
                     epub_file=f"bulk-{n}.epub", size_bytes=10)
        (config.epub_dir / f"bulk-{n}.epub").write_bytes(b"x" * 10)
        s.add(ed)
        s.flush()
        d = Delivery(edition_id=ed.id, complete=True, bytes_sent=10,
                     completed_at=datetime.now(timezone.utc) - timedelta(hours=1))
        s.add(d)

with session_scope() as s:
    try:
        n = editions.confirm_due_deliveries(s)
        truthy(f"confirmed {n} deliveries without raising", True)
    except Exception as exc:
        truthy(f"confirmed deliveries without raising ({exc})", False)

with session_scope() as s:
    left = [e.epub_file for e in s.query(Edition)
            .filter(Edition.title.like("Bulk %")).all()]
    check("all bulk editions pruned to no file", left, [None, None, None])
    put(s, "keep_delivered_editions", 5)

# The build job as a whole must survive it.
import app.jobs as jobsmod  # noqa: E402
jobsmod.run_build()
with session_scope() as s:
    last = (s.query(JobRun).filter(JobRun.job == "build")
            .order_by(JobRun.id.desc()).first())
    check("scheduled build job succeeds", last.status.value, "ok")
    print("        message:", last.message)

print("\n[24] HTTP basic authentication")
import base64 as _b64  # noqa: E402

_cfg = _config


def _auth(user, password):
    raw = f"{user}:{password}".encode("utf-8")
    return {"Authorization": "Basic " + _b64.b64encode(raw).decode("ascii")}


# Off by default: no credentials configured means no challenge.
check("open when no user is set", client.get("/").status_code, 200)
check("opds open when no user is set", client.get("/opds").status_code, 200)

_cfg.web_user, _cfg.web_password = "reader", "s3cret"
try:
    r = client.get("/")
    check("web UI now demands credentials", r.status_code, 401)
    truthy("challenge names a realm",
           "basic" in r.headers.get("www-authenticate", "").lower())
    check("wrong password rejected",
          client.get("/", headers=_auth("reader", "nope")).status_code, 401)
    check("wrong user rejected",
          client.get("/", headers=_auth("nobody", "s3cret")).status_code, 401)
    check("correct credentials accepted",
          client.get("/", headers=_auth("reader", "s3cret")).status_code, 200)
    check("malformed header rejected",
          client.get("/", headers={"Authorization": "Basic !!!"}).status_code, 401)
    check("non-basic scheme rejected",
          client.get("/", headers={"Authorization": "Bearer xyz"}).status_code, 401)
    check("header with no colon rejected",
          client.get("/", headers={"Authorization": "Basic " +
                                   _b64.b64encode(b"readeronly").decode()}
                     ).status_code, 401)

    # OPDS inherits the web credentials when it has none of its own.
    check("opds inherits web auth", client.get("/opds").status_code, 401)
    check("opds accepts the web credentials",
          client.get("/opds", headers=_auth("reader", "s3cret")).status_code, 200)

    # A separate, shorter credential for the device.
    _cfg.opds_user, _cfg.opds_password = "kobo", "1234"
    check("opds uses its own credentials",
          client.get("/opds", headers=_auth("kobo", "1234")).status_code, 200)
    check("the admin credential still opens opds",
          client.get("/opds", headers=_auth("reader", "s3cret")).status_code, 200)
    check("web UI unaffected by the opds pair",
          client.get("/", headers=_auth("reader", "s3cret")).status_code, 200)
    _cfg.opds_user, _cfg.opds_password = "", ""

    # Escape hatch for readers that cannot authenticate.
    _cfg.opds_public = True
    check("opds_public leaves the catalogue open",
          client.get("/opds").status_code, 200)
    check("web UI still protected while opds is public",
          client.get("/").status_code, 401)
    _cfg.opds_public = False

    # A password with an accent must authenticate, not crash. compare_digest
    # raises TypeError on non-ASCII str, which would have been a 500.
    _cfg.web_password = "sença-fácil"
    check("non-ascii password accepted",
          client.get("/", headers=_auth("reader", "sença-fácil")).status_code, 200)
    check("non-ascii password still rejects a wrong one",
          client.get("/", headers=_auth("reader", "senca-facil")).status_code, 401)

    # The health endpoint stays open so the container healthcheck works.
    check("healthz stays unauthenticated",
          client.get("/healthz").status_code, 200)
finally:
    _cfg.web_user, _cfg.web_password = "", ""
    _cfg.opds_user, _cfg.opds_password = "", ""
    _cfg.opds_public = False

check("auth off again after the test", client.get("/").status_code, 200)

print("\n[25] timestamps are stored in UTC and displayed in the chosen zone")
from datetime import datetime as _dt  # noqa: E402

from app import timeutil  # noqa: E402

_stored = _dt(2026, 9, 8, 22, 30, 0)  # naive, as SQLite hands it back
check("default zone is UTC", timeutil.set_display_timezone("UTC"), "UTC")
check("UTC renders unchanged", timeutil.format_display(_stored), "08 Sep 22:30")

check("a real zone is accepted",
      timeutil.set_display_timezone("Asia/Dubai"), "Asia/Dubai")
check("UTC+4 shifts the clock and the date",
      timeutil.format_display(_stored), "09 Sep 02:30")
check("naive input is treated as UTC, not as local",
      timeutil.to_display(_stored).utcoffset().total_seconds(), 4 * 3600)

check("an aware timestamp converts too",
      timeutil.format_display(_stored.replace(tzinfo=timezone.utc)),
      "09 Sep 02:30")
check("None renders as a dash", timeutil.format_display(None), "—")

# A bad zone must not take the page down.
check("nonsense zone falls back to UTC",
      timeutil.set_display_timezone("Mars/Olympus"), "UTC")
check("blank falls back to UTC", timeutil.set_display_timezone(""), "UTC")

# now() follows the display zone, so an edition built late in the evening
# carries the local date rather than tomorrow's UTC one.
timeutil.set_display_timezone("Asia/Dubai")
check("now() is in the display zone",
      timeutil.now().utcoffset().total_seconds(), 4 * 3600)

# And the whole way through: the filter the templates actually use.
r = client.get("/jobs")
check("jobs page renders with a zone set", r.status_code, 200)
timeutil.set_display_timezone("UTC")

print("\n[26] the web UI never depends on OPDS credentials")
# Regression: the Categories page embedded <img src="/opds/cover/N.jpg">.
# Once OPDS had its own credentials that sat behind a different realm, so
# every thumbnail made the browser prompt again.
_config.web_user, _config.web_password = "admin", "webpass"
_config.opds_user, _config.opds_password = "device", "devpass"
try:
    web = _auth("admin", "webpass")
    page = client.get("/categories", headers=web)
    check("categories page loads", page.status_code, 200)

    srcs = set(re.findall(r'src="(/[^"]+)"', page.text))
    opds_srcs = {s for s in srcs if s.startswith("/opds")}
    check("no OPDS URLs embedded in the page", opds_srcs, set())
    truthy("covers are served from a web path",
           any(s.endswith("/cover.jpg") for s in srcs))

    # Every subresource the page references must load with the web credential.
    for src in sorted(srcs):
        code = client.get(src, headers=web).status_code
        check(f"subresource {src} loads with web auth", code, 200)

    # Same for the Editions page download link.
    eds = client.get("/editions", headers=web)
    links = set(re.findall(r'href="(/editions/\d+/download\.epub)"', eds.text))
    truthy("editions page offers a web-auth download", links)
    for link in sorted(links):
        check(f"{link} loads with web auth",
              client.get(link, headers=web).status_code, 200)
        check(f"{link} rejects no credentials",
              client.get(link).status_code, 401)

    # Downloading from the admin UI is a look, not a sync: it must not count.
    with session_scope() as s:
        before = s.query(Delivery).count()
    for link in sorted(links):
        client.get(link, headers=web)
    with session_scope() as s:
        check("admin download records no delivery",
              s.query(Delivery).count(), before)
finally:
    _config.web_user = _config.web_password = ""
    _config.opds_user = _config.opds_password = ""

print("\n[27] renaming the project must not lose the database")
import os as _os  # noqa: E402

from app.db import LEGACY_DB_NAME, adopt_legacy_database  # noqa: E402
from app.main import _warn_about_legacy_env  # noqa: E402

import tempfile  # noqa: E402

# Exercise the rename in a scratch directory: the live database is held open
# by SQLite, and on Windows an open file cannot be renamed at all.
_saved_dir = config.data_dir
with tempfile.TemporaryDirectory() as _tmp:
    config.data_dir = Path(_tmp)
    _legacy = config.db_path.with_name(LEGACY_DB_NAME)
    _current = config.db_path
    _payload = b"SQLite format 3\x00 pretend database"

    # A database written under the old name must be adopted, not ignored.
    _legacy.write_bytes(_payload)
    _legacy.with_name(_legacy.name + "-wal").write_bytes(b"wal")
    adopt_legacy_database()
    check("legacy database adopted under the new name",
          (_legacy.exists(), _current.exists()), (False, True))
    check("contents preserved byte for byte",
          _current.read_bytes(), _payload)
    truthy("the -wal sidecar came too",
           _current.with_name(_current.name + "-wal").exists())

    # With a real database already there, a stray legacy file is left alone.
    _legacy.write_bytes(b"decoy")
    adopt_legacy_database()
    check("existing database is never overwritten",
          _current.read_bytes(), _payload)
    truthy("stray legacy file left untouched", _legacy.exists())

    # Nothing to adopt is a silent no-op.
    _legacy.unlink()
    adopt_legacy_database()
    check("no legacy file is a no-op", _current.read_bytes(), _payload)
config.data_dir = _saved_dir

# A stale env prefix must be shouted about, since it silently disables auth.
_os.environ["RSSOSPD_WEB_USER"] = "stale"
try:
    import logging as _logging

    class _Catcher(_logging.Handler):
        def __init__(self):
            super().__init__()
            self.messages = []

        def emit(self, record):
            self.messages.append(record.getMessage())

    _catch = _Catcher()
    _logging.getLogger("rssopds").addHandler(_catch)
    _warn_about_legacy_env()
    _logging.getLogger("rssopds").removeHandler(_catch)
    truthy("stale RSSOSPD_ prefix is reported",
           any("RSSOSPD_WEB_USER" in m for m in _catch.messages))
finally:
    del _os.environ["RSSOSPD_WEB_USER"]

print("\n[28] a downloaded edition is never superseded out from under the reader")
# Four real editions were superseded despite a completed download: their
# articles went back to the pool and reappeared, unread, in the next book.
# Two routes caused it -- the confirm delay not having elapsed when a rebuild
# ran, and supersede_available() sweeping unconditionally on startup.
with session_scope() as s:
    put(s, "delivery_confirm_delay_s", 3600)   # deliberately NOT yet due
    cat3 = Category(name="Race", slug="race")
    s.add(cat3)
    s.flush()

    def _edition(number, articles, downloaded):
        ed = Edition(category_id=cat3.id, number=number, title=f"Race {number}",
                     state=EditionState.available, epub_file=f"race-{number}.epub",
                     size_bytes=10, article_count=len(articles))
        (config.epub_dir / f"race-{number}.epub").write_bytes(b"x" * 10)
        s.add(ed)
        s.flush()
        for pos, art in enumerate(articles):
            art.state = ArticleState.published
            s.add(EditionArticle(edition_id=ed.id, article_id=art.id,
                                 position=pos))
        if downloaded:
            s.add(Delivery(edition_id=ed.id, complete=True, bytes_sent=10,
                           completed_at=datetime.now(timezone.utc)))
        return ed

    read_arts = []
    for n in range(2):
        a = Article(feed_id=feed_id, category_id=cat3.id, title=f"Downloaded {n}",
                    body_html="<p>Body.</p>", word_count=2,
                    state=ArticleState.ready)
        s.add(a)
        read_arts.append(a)
    unread_arts = []
    for n in range(2):
        a = Article(feed_id=feed_id, category_id=cat3.id, title=f"Untouched {n}",
                    body_html="<p>Body.</p>", word_count=2,
                    state=ArticleState.ready)
        s.add(a)
        unread_arts.append(a)
    s.flush()
    downloaded_id = _edition(1, read_arts, downloaded=True).id
    untouched_id = _edition(2, unread_arts, downloaded=False).id
    read_ids = [a.id for a in read_arts]
    unread_ids = [a.id for a in unread_arts]

from app.pipeline.editions import _supersede, was_fully_downloaded  # noqa: E402

with session_scope() as s:
    check("the downloaded edition is recognised as such",
          was_fully_downloaded(s.get(Edition, downloaded_id)), True)
    retired = _supersede(s, s.get(Edition, downloaded_id))
    check("it is NOT superseded", retired, False)

with session_scope() as s:
    check("it became delivered instead",
          s.get(Edition, downloaded_id).state, EditionState.delivered)
    states = [s.get(Article, i).state for i in read_ids]
    check("its articles are marked read, not returned to the pool",
          states, [ArticleState.delivered, ArticleState.delivered])

with session_scope() as s:
    retired = _supersede(s, s.get(Edition, untouched_id))
    check("an untouched edition IS still superseded", retired, True)

with session_scope() as s:
    check("untouched edition retired",
          s.get(Edition, untouched_id).state, EditionState.superseded)
    states = [s.get(Article, i).state for i in unread_ids]
    check("its articles go back to the pool",
          states, [ArticleState.ready, ArticleState.ready])

print("\n[29] the startup sweep also spares a downloaded edition")
with session_scope() as s:
    ed = s.get(Edition, downloaded_id)
    ed.state = EditionState.available          # pretend it is current again
    ed.epub_file = "race-1.epub"
    for i in read_ids:
        s.get(Article, i).state = ArticleState.published
with session_scope() as s:
    # Other categories may also have current editions; count only the ones
    # that genuinely have nothing downloaded, which is what should be retired.
    available = s.query(Edition).filter(
        Edition.state == EditionState.available).all()
    expected = sum(1 for e in available if not editions.was_fully_downloaded(e))
    ids_spared = [e.id for e in available if editions.was_fully_downloaded(e)]
with session_scope() as s:
    swept = editions.supersede_available(s)
check("the sweep retired exactly the undownloaded editions", swept, expected)
truthy("and at least one edition was spared", ids_spared)

with session_scope() as s:
    check("the downloaded edition was spared",
          s.get(Edition, downloaded_id).state, EditionState.delivered)
    check("its articles stayed read",
          [s.get(Article, i).state for i in read_ids],
          [ArticleState.delivered, ArticleState.delivered])
    put(s, "delivery_confirm_delay_s", 0)

print("\n[30] the editions page copes with several downloads per edition")
from app.web.routes import download_summary, short_device  # noqa: E402

check("version stripped from a device name",
      short_device("CrossPoint-ESP32-1.6.0"), "CrossPoint-ESP32")
check("slash-versioned agent", short_device("KOReader/2024.04"), "KOReader")
check("curl", short_device("curl/8.21.0"), "curl")
check("browser agent takes the first token",
      short_device("Mozilla/5.0 (Windows NT 10.0; Win64)"), "Mozilla")
check("empty agent", short_device(""), "unknown")

with session_scope() as s:
    cat4 = Category(name="Busy", slug="busy")
    s.add(cat4)
    s.flush()
    ed = Edition(category_id=cat4.id, number=1, title="Busy 1",
                 state=EditionState.delivered, epub_file="busy.epub",
                 size_bytes=1000, article_count=1)
    s.add(ed)
    s.flush()
    # Four clients, deliberately sharing an IP, as happened in practice.
    for ua, sent, done in [("KOReader/2024.04", 1000, True),
                           ("curl/8.21.0", 250, False),
                           ("CrossPoint-ESP32-1.6.0", 1000, True),
                           ("FlakyReader/0.1", 0, False)]:
        s.add(Delivery(edition_id=ed.id, user_agent=ua, client_ip="10.0.0.5",
                       bytes_sent=sent, complete=done))
    busy_id = ed.id

with session_scope() as s:
    ed = s.get(Edition, busy_id)
    summary = download_summary(s, [ed])[busy_id]
    check("one entry per delivery", len(summary), 4)
    check("completed transfers sort first",
          [e["complete"] for e in summary], [True, True, False, False])
    check("then by how far they got",
          [e["pct"] for e in summary], [100, 100, 25, 0])
    check("devices are distinguishable despite one IP",
          sorted({e["device"] for e in summary}),
          ["CrossPoint-ESP32", "FlakyReader", "KOReader", "curl"])
    check("a zero-size edition does not divide by zero",
          download_summary(s, [Edition(category_id=cat4.id, number=9,
                                       title="z", size_bytes=0)]).popitem()[1],
          [])

r = client.get("/editions")
check("editions page renders", r.status_code, 200)
truthy("the column shows device names, not just repeated IPs",
       "KOReader" in r.text and "CrossPoint-ESP32" in r.text)
truthy("only three are listed inline, the rest collapsed",
       "+1 more" in r.text)
truthy("the full agent is available on hover", "10.0.0.5" in r.text)

print("\n[31] the issue number only advances once an edition is delivered")
with session_scope() as s:
    put(s, "delivery_confirm_delay_s", 0)
    cat5 = Category(name="Numbering", slug="numbering")
    s.add(cat5)
    s.flush()
    num_feed = Feed(title="Numbering feed", kind=SourceKind.rss, url="http://x",
                    category_id=cat5.id, clean_with_ai=False,
                    extract_fulltext=False, include_images=False)
    s.add(num_feed)
    s.flush()
    num_feed_id, num_cat_id = num_feed.id, cat5.id


def _add_ready(title, cat_id=None, feed_id=None):
    with session_scope() as s:
        s.add(Article(feed_id=feed_id or num_feed_id, category_id=cat_id or num_cat_id,
                      title=title, body_html=f"<p>{title}</p>", word_count=2,
                      state=ArticleState.ready))


def _current(cat_id):
    with session_scope() as s:
        return (s.query(Edition)
                .filter(Edition.category_id == cat_id,
                       Edition.state == EditionState.available)
                .one())


# First edition for a new category: always No. 1.
_add_ready("N1")
with session_scope() as s:
    editions.build_all(s)
first = _current(num_cat_id)
check("first edition is number 1", first.number, 1)
first_id = first.id

# A second article arrives before anyone has downloaded it: the rebuild must
# REUSE the number, not advance it -- this is the actual behaviour change.
_add_ready("N2")
with session_scope() as s:
    editions.build_all(s)
second = _current(num_cat_id)
check("undelivered rebuild reuses the same number", second.number, 1)
truthy("but it is a genuinely different edition row", second.id != first_id)
with session_scope() as s:
    check("the old edition was actually superseded",
          s.get(Edition, first_id).state, EditionState.superseded)

# It can keep reusing the number across several undelivered rebuilds in a row.
_add_ready("N3")
with session_scope() as s:
    editions.build_all(s)
check("still number 1 after a second undelivered rebuild",
      _current(num_cat_id).number, 1)

# Now the reader actually downloads it, in full, over an OPDS request.
current = _current(num_cat_id)
r = client.get(f"/opds/edition/{current.id}.epub")
check("download completes", r.status_code, 200)
from app.pipeline.editions import was_fully_downloaded  # noqa: E402

with session_scope() as s:
    truthy("delivery recorded as complete",
           was_fully_downloaded(s.get(Edition, current.id)))

# The next rebuild must now advance past 1, since that edition was delivered.
_add_ready("N4")
with session_scope() as s:
    editions.build_all(s)
check("number advances once the previous one was downloaded",
      _current(num_cat_id).number, 2)
with session_scope() as s:
    check("edition 1 ended up delivered, not superseded",
          s.get(Edition, current.id).state, EditionState.delivered)

# An admin "Mark read" must count the same as a real download.
_add_ready("N5")
with session_scope() as s:
    editions.build_all(s)
check("second undelivered edition also reuses its number",
      _current(num_cat_id).number, 2)
with session_scope() as s:
    editions.mark_delivered(s, s.get(Edition, _current(num_cat_id).id))
_add_ready("N6")
with session_scope() as s:
    editions.build_all(s)
check("admin mark-read advances the number too",
      _current(num_cat_id).number, 3)

with session_scope() as s:
    put(s, "delivery_confirm_delay_s", 60)

print("\n[32] the number-backfill migration does not run twice")
from app.db import _backfill_edition_numbers  # noqa: E402
from app.models import Setting  # noqa: E402

with session_scope() as s:
    check("the migration marker is set after init_db()",
          s.get(Setting, "_schema_edition_numbers_backfilled") is not None, True)

# Simulate exactly the state this feature now creates on purpose: two
# editions in one category sharing a number. Without the guard, re-running
# the old heuristic would see "not distinct" and renumber them, undoing the
# reuse behaviour on every restart.
with session_scope() as s:
    rows = (s.query(Edition)
            .filter(Edition.category_id == num_cat_id)
            .order_by(Edition.id).all())
    before = [(e.id, e.number) for e in rows]
    truthy("fixture actually has a repeated number to protect",
           len({n for _i, n in before}) < len(before))

with session_scope() as s:
    _backfill_edition_numbers(s)

with session_scope() as s:
    after = [(e.id, e.number) for e in
            s.query(Edition).filter(Edition.category_id == num_cat_id)
            .order_by(Edition.id).all()]
    check("a second migration run leaves intentional duplicates untouched",
          after, before)

print("\n[33] AI settings: the model picker route and the prompt guard")
from app.pipeline import clean as clean_mod  # noqa: E402
from app.settings_store import get as sget  # noqa: E402

r = client.get("/settings")
truthy("backend picker is on the page", 'name="ai_backend"' in r.text)
truthy("the cleanup prompt textarea is on the page",
       'id="field-ai_clean_prompt"' in r.text)
truthy("a reset-to-default button is on the page",
       "Reset to default prompt" in r.text)

# Every field the page's own JS looks up by id must actually carry that id --
# a plain <input> once lost its id="field-..." (only the textarea kept it),
# which made "Fetch available models" and "Test model" throw
# "Cannot read properties of null" instead of doing anything.
for settings_key in ("ollama_url", "ollama_model", "openai_base_url",
                     "openai_model", "openai_api_key", "ollama_timeout_s",
                     "ai_backend"):
    truthy(f"field-{settings_key} id is present for the page's JS to find",
          f'id="field-{settings_key}"' in r.text)

with session_scope() as s:
    prompt_before = sget(s, "ai_clean_prompt")
r = client.post("/settings", data={"ai_clean_prompt": "no placeholder here"},
                follow_redirects=False)
truthy("saving a prompt without {chunk} is rejected with a warning",
       "chunk" in r.headers.get("location", ""))
with session_scope() as s:
    check("the stored prompt is untouched by the rejected save",
          sget(s, "ai_clean_prompt"), prompt_before)

r = client.post("/settings", data={
    "ai_clean_prompt": "Custom prompt. Text: {chunk}",
    "ai_min_words": "7",
    "ai_backend": "ollama",
}, follow_redirects=False)
with session_scope() as s:
    check("a valid custom prompt is saved",
          sget(s, "ai_clean_prompt"), "Custom prompt. Text: {chunk}")
    check("the word threshold is saved", sget(s, "ai_min_words"), 7)
    put(s, "ai_clean_prompt", prompt_before)
    put(s, "ai_min_words", 40)


class _FakeModelsResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


_real_httpx_get = clean_mod.httpx.get
clean_mod.httpx.get = lambda url, headers=None, timeout=None, **kw: (
    _FakeModelsResponse({"models": [{"name": "llama3.2"}]}))
r = client.get("/settings/ai/models", params={"backend": "ollama", "base_url": "http://x:11434"})
clean_mod.httpx.get = _real_httpx_get
check("the model-picker route reports models found", r.json(), {"models": ["llama3.2"]})

r = client.get("/settings/ai/models", params={"backend": "ollama", "base_url": ""})
check("an empty base URL is rejected without a network call", r.status_code, 400)

print("\n[33b] the settings page's \"Test model\" button")
import httpx  # noqa: E402


class _FakeGenerateResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


truthy("Test model button is on the settings page",
      client.get("/settings").text.count("Test model") == 2)

_real_httpx_post = clean_mod.httpx.post
clean_mod.httpx.post = lambda *a, **k: _FakeGenerateResponse(
    {"response": "Hello, working, I am llama3.2."})
r = client.get("/settings/ai/test",
               params={"backend": "ollama", "base_url": "http://x:11434", "model": "llama3.2"})
clean_mod.httpx.post = _real_httpx_post
check("a working model reports its reply", r.json(),
      {"reply": "Hello, working, I am llama3.2."})

clean_mod.httpx.post = lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("refused"))
r = client.get("/settings/ai/test",
               params={"backend": "ollama", "base_url": "http://dead:11434", "model": "llama3.2"})
clean_mod.httpx.post = _real_httpx_post
check("an unreachable server is reported, not a 500", r.status_code, 502)

r = client.get("/settings/ai/test",
               params={"backend": "ollama", "base_url": "http://x:11434", "model": ""})
check("a blank model is rejected without a network call", r.status_code, 400)

print("\n[34] the dashboard counts how articles were cleaned")
import re as _re  # noqa: E402


def _dashboard_stat(label):
    body = client.get("/").text
    idx = body.index(label)
    return int(_re.search(r"<b>(\d+)</b>", body[max(0, idx - 60):idx]).group(1))


before_ai = _dashboard_stat("cleaned with AI")
before_fallback = _dashboard_stat("cleaned with the rule-based fallback")

with session_scope() as s:
    dash_cat = Category(name="Dashboard Test", slug="dashboard-test")
    s.add(dash_cat)
    s.flush()
    dash_feed = Feed(title="Dashboard Feed", kind=SourceKind.rss, url="http://x",
                     category_id=dash_cat.id)
    s.add(dash_feed)
    s.flush()
    for by in ("qwen2.5:7b-instruct", "gpt-4o", "rules", "rules", None):
        s.add(Article(feed_id=dash_feed.id, category_id=dash_cat.id, title="t",
                      body_html="<p>x</p>", word_count=1,
                      state=ArticleState.ready, cleaned_by=by))

check("AI-cleaned count includes every non-rules model",
      _dashboard_stat("cleaned with AI") - before_ai, 2)
check("fallback count only counts \"rules\", not the untouched article",
      _dashboard_stat("cleaned with the rule-based fallback") - before_fallback, 2)

print("\n" + "=" * 60)
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): {FAILS}")
    sys.exit(1)
print("ALL CHECKS PASSED")
