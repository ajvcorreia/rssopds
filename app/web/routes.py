"""Web UI. Every configuration option in this app is set from here."""
from __future__ import annotations

import logging
import re
import shutil
from datetime import timedelta, timezone
from pathlib import Path
from urllib.parse import quote as urlquote

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Request, UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

import httpx

from .. import backup, ebooks, jobs, scheduler
from ..config import config
from ..db import get_session
from ..models import (
    Article, ArticleState, Category, Edition, EditionState, Feed, FeedItem,
    JobRun, SourceKind, ThreadMode, utcnow,
)
from ..opds import access_log
from ..pipeline import clean, covers, ebook_covers, editions as editions_mod
from ..security import require_web
from .. import timeutil
from ..settings_store import DEFAULTS, all_settings, grouped, put
from ..sources import fediverse

log = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_web)])
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["localtime"] = timeutil.format_display


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "category"


def render(request: Request, template: str, **ctx) -> HTMLResponse:
    ctx.setdefault("request", request)
    ctx.setdefault("flash", request.query_params.get("msg"))
    ctx.setdefault("error", request.query_params.get("err"))
    return templates.TemplateResponse(template, ctx)


def back(path: str, msg: str = "", err: str = "") -> RedirectResponse:
    from urllib.parse import urlencode
    query = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


# --- dashboard ------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, session: Session = Depends(get_session)):
    state_counts = dict(session.execute(
        select(Article.state, func.count(Article.id)).group_by(Article.state)
    ).all())

    held = session.execute(
        select(func.count(Article.id)).where(
            Article.state == ArticleState.pending,
            Article.thread_open_until.isnot(None),
            Article.thread_open_until > utcnow(),
        )
    ).scalar_one()

    cleaned_by_counts = dict(session.execute(
        select(Article.cleaned_by, func.count(Article.id))
        .where(Article.cleaned_by.isnot(None))
        .group_by(Article.cleaned_by)
    ).all())
    ai_cleaned = sum(n for by, n in cleaned_by_counts.items() if by != "rules")
    fallback_cleaned = cleaned_by_counts.get("rules", 0)

    catalog = editions_mod.catalog_categories(session)
    feeds = list(session.execute(select(Feed).order_by(Feed.title)).scalars())
    recent = list(session.execute(
        select(JobRun).order_by(JobRun.started_at.desc()).limit(12)
    ).scalars())

    return render(request, "dashboard.html",
                  counts=state_counts, held=held, catalog=catalog,
                  feeds=feeds, recent=recent, ArticleState=ArticleState,
                  ai_cleaned=ai_cleaned, fallback_cleaned=fallback_cleaned,
                  scheduler_running=scheduler.scheduler.running)


@router.post("/run")
def run_now(background: bool = True):
    scheduler.scheduler.add_job(jobs.run_pipeline_once, id="manual-run",
                                replace_existing=True)
    return back("/", "Pipeline run started.")


# --- feeds ----------------------------------------------------------------

@router.get("/feeds", response_class=HTMLResponse)
def feed_list(request: Request, session: Session = Depends(get_session)):
    feeds = list(session.execute(select(Feed).order_by(Feed.title)).scalars())
    counts = dict(session.execute(
        select(FeedItem.feed_id, func.count(FeedItem.id)).group_by(FeedItem.feed_id)
    ).all())
    return render(request, "feeds.html", feeds=feeds, counts=counts)


@router.get("/feeds/new", response_class=HTMLResponse)
def feed_new(request: Request, session: Session = Depends(get_session)):
    categories = list(session.execute(
        select(Category).order_by(Category.name)).scalars())
    return render(request, "feed_form.html", feed=None, categories=categories,
                  SourceKind=SourceKind, ThreadMode=ThreadMode)


@router.get("/feeds/{feed_id}/edit", response_class=HTMLResponse)
def feed_edit(feed_id: int, request: Request,
              session: Session = Depends(get_session)):
    feed = session.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(404, "no such feed")
    categories = list(session.execute(
        select(Category).order_by(Category.name)).scalars())
    return render(request, "feed_form.html", feed=feed, categories=categories,
                  SourceKind=SourceKind, ThreadMode=ThreadMode)


@router.post("/feeds/save")
def feed_save(
    session: Session = Depends(get_session),
    feed_id: str = Form(""),
    title: str = Form(...),
    kind: str = Form("rss"),
    url: str = Form(""),
    category_id: str = Form(""),
    enabled: str = Form(""),
    thread_mode: str = Form("none"),
    extract_fulltext: str = Form(""),
    clean_with_ai: str = Form(""),
    include_images: str = Form(""),
    poll_interval_min: int = Form(30),
    max_items_per_poll: int = Form(50),
    fedi_token: str = Form(""),
    fedi_list_id: str = Form(""),
    fedi_accounts: str = Form(""),
    fedi_skip_reblogs: str = Form(""),
):
    feed = session.get(Feed, int(feed_id)) if feed_id else Feed()
    if feed is None:
        raise HTTPException(404, "no such feed")

    feed.title = title.strip()
    feed.kind = SourceKind(kind)
    feed.url = url.strip()
    feed.category_id = int(category_id) if category_id else None
    feed.enabled = bool(enabled)
    feed.thread_mode = ThreadMode(thread_mode)
    feed.extract_fulltext = bool(extract_fulltext)
    feed.clean_with_ai = bool(clean_with_ai)
    feed.include_images = bool(include_images)
    feed.poll_interval_min = max(1, poll_interval_min)
    feed.max_items_per_poll = max(1, max_items_per_poll)
    feed.fedi_list_id = fedi_list_id.strip() or None
    feed.fedi_accounts = fedi_accounts.strip()
    feed.fedi_skip_reblogs = bool(fedi_skip_reblogs)
    # Blank means "leave the stored token alone", so editing a feed does not
    # wipe a credential the form never shows back.
    if fedi_token.strip():
        feed.fedi_token = fedi_token.strip()

    if not feed.id:
        session.add(feed)
    session.commit()
    scheduler.reschedule_feed(feed)
    return back("/feeds", f"Saved “{feed.title}”.")


@router.post("/feeds/{feed_id}/delete")
def feed_delete(feed_id: int, session: Session = Depends(get_session)):
    feed = session.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(404, "no such feed")
    name = feed.title
    scheduler.remove_feed(feed_id)
    session.delete(feed)
    session.commit()
    return back("/feeds", f"Deleted “{name}”.")


@router.post("/feeds/{feed_id}/poll")
def feed_poll(feed_id: int):
    scheduler.scheduler.add_job(jobs.run_poll, args=[feed_id],
                                id=f"manual-poll-{feed_id}", replace_existing=True)
    return back("/feeds", "Poll queued.")


@router.post("/feeds/{feed_id}/toggle")
def feed_toggle(feed_id: int, session: Session = Depends(get_session)):
    feed = session.get(Feed, feed_id)
    if feed is None:
        raise HTTPException(404, "no such feed")
    feed.enabled = not feed.enabled
    session.commit()
    scheduler.reschedule_feed(feed)
    verb = "Enabled" if feed.enabled else "Disabled"
    return back("/feeds", f"{verb} “{feed.title}”.")


# --- Threads / fediverse helper ------------------------------------------

@router.get("/threads", response_class=HTMLResponse)
def threads_help(request: Request, session: Session = Depends(get_session)):
    feeds = list(session.execute(
        select(Feed).where(Feed.kind == SourceKind.fediverse)
        .order_by(Feed.title)
    ).scalars())
    return render(request, "threads.html", feeds=feeds)


@router.post("/threads/verify")
def threads_verify(instance: str = Form(...), token: str = Form(...)):
    try:
        account = fediverse.verify(instance, token)
        found = fediverse.lists(instance, token)
    except Exception as exc:
        return back("/threads", err=str(exc))
    names = ", ".join(f"{lst['title']} (id {lst['id']})" for lst in found) or "none"
    return back("/threads",
                f"Token valid for @{account.get('acct')}. Lists: {names}")


@router.post("/threads/create-list")
def threads_create_list(instance: str = Form(...), token: str = Form(...),
                        list_title: str = Form("Threads")):
    try:
        created = fediverse.create_list(instance, token, list_title)
    except Exception as exc:
        return back("/threads", err=str(exc))
    return back("/threads",
                f"Created list “{created['title']}” with id {created['id']}. "
                f"Put that id in the feed's List ID field.")


@router.post("/threads/follow")
def threads_follow(instance: str = Form(...), token: str = Form(...),
                   handle: str = Form(...), list_id: str = Form("")):
    try:
        account = fediverse.follow_account(instance, token, handle,
                                           list_id.strip() or None)
    except Exception as exc:
        return back("/threads", err=str(exc))
    return back("/threads",
                f"Now following @{account.get('acct')}. Note that only posts "
                f"made from now on will arrive — the fediverse does not "
                f"backfill history.")


# --- categories -----------------------------------------------------------

@router.get("/categories", response_class=HTMLResponse)
def category_list(request: Request, session: Session = Depends(get_session)):
    categories = list(session.execute(
        select(Category).order_by(Category.sort_order, Category.name)).scalars())
    feed_counts = dict(session.execute(
        select(Feed.category_id, func.count(Feed.id)).group_by(Feed.category_id)
    ).all())
    unread = dict(session.execute(
        select(Article.category_id, func.count(Article.id))
        .where(Article.state.in_([ArticleState.ready, ArticleState.published]))
        .group_by(Article.category_id)
    ).all())
    return render(request, "categories.html", categories=categories,
                  feed_counts=feed_counts, unread=unread)


@router.post("/categories/save")
def category_save(session: Session = Depends(get_session),
                  category_id: str = Form(""), name: str = Form(...),
                  sort_order: int = Form(100),
                  cover: UploadFile | None = File(None)):
    category = session.get(Category, int(category_id)) if category_id else Category()
    if category is None:
        raise HTTPException(404, "no such category")

    category.name = name.strip()
    category.slug = slugify(category.name)
    category.sort_order = sort_order
    if not category.id:
        session.add(category)
    session.flush()

    if cover is not None and cover.filename:
        suffix = Path(cover.filename).suffix.lower()
        if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
            return back("/categories", err="Cover must be a JPG, PNG or WebP.")
        dest = config.cover_dir / f"cat-{category.id}{suffix}"
        with dest.open("wb") as fh:
            shutil.copyfileobj(cover.file, fh)
        category.cover_file = dest.name

    session.commit()
    return back("/categories", f"Saved “{category.name}”.")


@router.post("/categories/{category_id}/delete")
def category_delete(category_id: int, session: Session = Depends(get_session)):
    category = session.get(Category, category_id)
    if category is None:
        raise HTTPException(404, "no such category")
    name = category.name
    session.delete(category)
    session.commit()
    return back("/categories", f"Deleted “{name}”.")


@router.get("/categories/{category_id}/cover.jpg")
def category_cover(category_id: int, session: Session = Depends(get_session)):
    """Cover for the web UI, behind the *web* credentials.

    The Categories page used to embed the OPDS cover URL, which sits behind a
    different realm and, once OPDS has its own credentials, a different
    password -- so every thumbnail on the page triggered a fresh browser
    prompt. Nothing in the web UI should depend on OPDS auth.
    """
    from fastapi.responses import FileResponse

    category = session.get(Category, category_id)
    if category is None:
        raise HTTPException(404, "no such category")
    sizes = all_settings(session)
    path = covers.resolve(category, config.cover_dir, when=timeutil.now(),
                          width=sizes["image_max_width"],
                          height=sizes["image_max_height"])
    return FileResponse(path, media_type="image/jpeg")


@router.get("/editions/{edition_id}/download.epub")
def edition_download(edition_id: int, session: Session = Depends(get_session)):
    """Download an edition from the web UI, behind the web credentials.

    Deliberately does not record a Delivery: fetching a book from the admin
    page is a look, not a sync, and should never mark its articles read.
    """
    from fastapi.responses import FileResponse

    edition = session.get(Edition, edition_id)
    if edition is None or not edition.epub_file:
        raise HTTPException(404, "no such edition")
    path = config.epub_dir / edition.epub_file
    if not path.exists():
        raise HTTPException(410, "edition file has been pruned")
    return FileResponse(path, media_type="application/epub+zip",
                        filename=edition.epub_file)


@router.post("/categories/{category_id}/clear-cover")
def category_clear_cover(category_id: int, session: Session = Depends(get_session)):
    category = session.get(Category, category_id)
    if category is None:
        raise HTTPException(404, "no such category")
    if category.cover_file:
        (config.cover_dir / category.cover_file).unlink(missing_ok=True)
        category.cover_file = None
    session.commit()
    return back("/categories", "Reverted to a generated cover.")


# --- ebooks shelf -----------------------------------------------------------
# The dumb, read-only-over-OPDS mirror of data/ebooks/ (see app/ebooks.py and
# app/opds/routes.py). This gives it a write side: dropping files in used to
# require scp/docker cp onto the host, which is fine for the server operator
# but not for anyone else who just wants to add a book.

def _ebooks_folder(request: Request, subpath: str):
    target = ebooks.resolve(subpath)
    if target is None or not target.is_dir():
        return back("/ebooks", err="No such folder.")
    dirs, files = ebooks.list_dir(target)
    path = subpath.strip("/")
    parts = [p for p in path.split("/") if p]
    crumbs = []
    for i, part in enumerate(parts):
        crumbs.append((part, "/".join(parts[: i + 1])))
    return render(request, "ebooks.html", path=path, parts=parts, crumbs=crumbs,
                  dirs=dirs, files=files, folders=ebooks.list_folders())


@router.get("/ebooks/download/{subpath:path}")
def ebooks_download(subpath: str):
    """Download from the web UI, behind the web credentials.

    Declared before the folder browser below: both match GET
    /ebooks/{...:path}, and routes are matched in declaration order, so this
    has to come first or "download/whatever.epub" would be swallowed as a
    folder path. The OPDS download URL sits behind the OPDS realm, which can
    have its own, different password -- see category_cover for the same issue
    with covers.
    """
    from fastapi.responses import FileResponse

    target = ebooks.resolve(subpath)
    if target is None or not target.is_file():
        raise HTTPException(404, "no such file")
    return FileResponse(target, media_type=ebooks.guess_mime(target),
                        filename=target.name)


@router.get("/ebooks/cover/{subpath:path}")
def ebooks_cover(subpath: str):
    """A cached thumbnail of the file's own embedded cover, if it has one.

    Declared before the folder browser for the same reason as the download
    route above. 404s -- rather than a placeholder image -- for anything
    without an extractable cover, so the page's <img onerror> can just hide
    it and fall back to a plain filename.
    """
    target = ebooks.resolve(subpath)
    if target is None or not target.is_file():
        raise HTTPException(404, "no such file")
    thumb = ebook_covers.thumbnail(target, config.ebook_cover_dir)
    if thumb is None:
        raise HTTPException(404, "no cover")
    from fastapi.responses import FileResponse
    return FileResponse(thumb, media_type="image/jpeg")


@router.get("/ebooks", response_class=HTMLResponse)
@router.get("/ebooks/{subpath:path}", response_class=HTMLResponse)
def ebooks_folder(request: Request, subpath: str = ""):
    return _ebooks_folder(request, subpath)


@router.post("/ebooks/upload")
async def ebooks_upload(path: str = Form(""), files: list[UploadFile] = File(...)):
    target = ebooks.resolve(path)
    if target is None or not target.is_dir():
        return back("/ebooks", err="No such folder.")

    saved = 0
    for upload in files:
        name = Path(upload.filename or "").name
        if not name or not ebooks.is_safe_name(name):
            continue
        dest = target / name
        with dest.open("wb") as fh:
            shutil.copyfileobj(upload.file, fh)
        saved += 1

    dest_url = f"/ebooks/{urlquote(path)}" if path else "/ebooks"
    if saved == 0:
        return back(dest_url, err="No files were uploaded.")
    return back(dest_url, f"Uploaded {saved} file{'s' if saved != 1 else ''}.")


@router.post("/ebooks/mkdir")
def ebooks_mkdir(path: str = Form(""), name: str = Form(...)):
    target = ebooks.resolve(path)
    name = name.strip()
    dest_url = f"/ebooks/{urlquote(path)}" if path else "/ebooks"
    if target is None or not target.is_dir():
        return back("/ebooks", err="No such folder.")
    if not ebooks.is_safe_name(name):
        return back(dest_url, err="Not a valid folder name.")
    new_dir = target / name
    if new_dir.exists():
        return back(dest_url, err=f"“{name}” already exists.")
    new_dir.mkdir()
    return back(dest_url, f"Created “{name}”.")


@router.post("/ebooks/delete-file/{subpath:path}")
def ebooks_delete_file(subpath: str):
    target = ebooks.resolve(subpath)
    parent = subpath.rsplit("/", 1)[0] if "/" in subpath else ""
    dest_url = f"/ebooks/{urlquote(parent)}" if parent else "/ebooks"
    if target is None or not target.is_file():
        return back(dest_url, err="No such file.")
    name = target.name
    target.unlink()
    return back(dest_url, f"Deleted “{name}”.")


@router.post("/ebooks/delete-folder/{subpath:path}")
def ebooks_delete_folder(subpath: str):
    parent = subpath.rsplit("/", 1)[0] if "/" in subpath else ""
    dest_url = f"/ebooks/{urlquote(parent)}" if parent else "/ebooks"
    target = ebooks.resolve(subpath)
    if target is None or not target.is_dir() or not subpath.strip("/"):
        return back(dest_url, err="No such folder.")
    name = target.name
    shutil.rmtree(target)
    return back(dest_url, f"Deleted “{name}” and everything in it.")


def _bulk_selection(path: str, files: list[str],
                    dirs: list[str]) -> tuple[Path | None, list[tuple[str, Path]]]:
    """Resolve checked filenames (relative to `path`) into real paths.

    Both bulk-delete and bulk-move take the same shape of form data: the
    current folder, plus the "files" and "dirs" checkboxes checked within it.
    """
    base = ebooks.resolve(path)
    if base is None or not base.is_dir():
        return None, []
    items: list[tuple[str, Path]] = []
    for name in files:
        if ebooks.is_safe_name(name):
            items.append(("file", base / name))
    for name in dirs:
        if ebooks.is_safe_name(name):
            items.append(("dir", base / name))
    return base, items


@router.post("/ebooks/bulk-delete")
def ebooks_bulk_delete(path: str = Form(""), files: list[str] = Form([]),
                       dirs: list[str] = Form([])):
    dest_url = f"/ebooks/{urlquote(path)}" if path else "/ebooks"
    base, items = _bulk_selection(path, files, dirs)
    if base is None:
        return back("/ebooks", err="No such folder.")
    if not items:
        return back(dest_url, err="Nothing selected.")

    deleted = 0
    for kind, target in items:
        if kind == "file" and target.is_file():
            target.unlink()
            deleted += 1
        elif kind == "dir" and target.is_dir():
            shutil.rmtree(target)
            deleted += 1
    return back(dest_url, f"Deleted {deleted} item{'s' if deleted != 1 else ''}.")


@router.post("/ebooks/bulk-move")
def ebooks_bulk_move(path: str = Form(""), dest: str = Form(""),
                     files: list[str] = Form([]), dirs: list[str] = Form([])):
    dest_url = f"/ebooks/{urlquote(path)}" if path else "/ebooks"
    base, items = _bulk_selection(path, files, dirs)
    if base is None:
        return back("/ebooks", err="No such folder.")
    if not items:
        return back(dest_url, err="Nothing selected.")
    target_dir = ebooks.resolve(dest)
    if target_dir is None or not target_dir.is_dir():
        return back(dest_url, err="No such destination folder.")

    moved = skipped = 0
    for kind, source in items:
        # Moving a folder into itself or one of its own subfolders would
        # corrupt the tree -- shutil.move refuses this too, but checking up
        # front lets the whole batch continue instead of raising partway in.
        if kind == "dir" and (not source.is_dir() or target_dir == source
                              or source in target_dir.parents):
            skipped += 1
            continue
        if kind == "file" and not source.is_file():
            skipped += 1
            continue
        new_path = target_dir / source.name
        if new_path == source or new_path.exists():
            skipped += 1
            continue
        shutil.move(str(source), str(new_path))
        moved += 1

    msg = f"Moved {moved} item{'s' if moved != 1 else ''}."
    if skipped:
        msg += f" Skipped {skipped} (already there or a name clash)."
    return back(dest_url, msg)


# --- articles -------------------------------------------------------------

@router.get("/articles", response_class=HTMLResponse)
def article_list(request: Request, state: str = "", limit: int = 100,
                 session: Session = Depends(get_session)):
    query = select(Article).order_by(Article.created_at.desc()).limit(limit)
    if state:
        query = query.where(Article.state == ArticleState(state))
    articles = list(session.execute(query).scalars())
    return render(request, "articles.html", articles=articles, state=state,
                  ArticleState=ArticleState, now=utcnow())


@router.get("/articles/{article_id}", response_class=HTMLResponse)
def article_detail(article_id: int, request: Request,
                   session: Session = Depends(get_session)):
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "no such article")
    return render(request, "article_detail.html", article=article)


@router.post("/articles/{article_id}/state")
def article_set_state(article_id: int, state: str = Form(...),
                      session: Session = Depends(get_session)):
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "no such article")
    article.state = ArticleState(state)
    if article.state != ArticleState.delivered:
        article.delivered_at = None
    session.commit()
    return back("/articles", f"Article {article_id} set to {state}.")


@router.post("/articles/{article_id}/release-thread")
def article_release_thread(article_id: int,
                           session: Session = Depends(get_session)):
    """Publish a held self-thread now instead of waiting out its window."""
    article = session.get(Article, article_id)
    if article is None:
        raise HTTPException(404, "no such article")
    article.thread_open_until = None
    session.commit()
    return back("/articles", "Thread released; it will be processed next pass.")


# --- editions -------------------------------------------------------------

def short_device(user_agent: str) -> str:
    """A recognisable name for a client, from its user-agent.

    The downloads column used to show only the IP, so three different readers
    behind one address were three identical-looking lines.
    """
    ua = (user_agent or "").strip()
    if not ua:
        return "unknown"
    first = ua.split()[0]
    name = first.split("/")[0]
    # "CrossPoint-ESP32-1.6.0" has its version in the name, not after a slash.
    name = re.sub(r"[-_]?v?\d+([._]\d+)*$", "", name)
    return (name or first)[:18]


def download_summary(session: Session, editions: list[Edition]) -> dict:
    """One compact line per client, so the column stays a single row tall."""
    summary: dict[int, list[dict]] = {}
    for edition in editions:
        entries = []
        for d in edition.deliveries:
            pct = (100 * d.bytes_sent / edition.size_bytes
                   if edition.size_bytes else 0)
            entries.append({
                "device": short_device(d.user_agent),
                "ip": d.client_ip or "?",
                "ua": d.user_agent or "",
                "pct": round(pct),
                "complete": bool(d.complete),
                "started": d.started_at,
            })
        # Finished transfers first, then furthest along.
        entries.sort(key=lambda e: (not e["complete"], -e["pct"]))
        summary[edition.id] = entries
    return summary


@router.get("/editions", response_class=HTMLResponse)
def edition_list(request: Request, session: Session = Depends(get_session)):
    rows = list(session.execute(
        select(Edition).options(joinedload(Edition.category))
        .order_by(Edition.created_at.desc()).limit(60)
    ).scalars())
    return render(request, "editions.html", editions=rows,
                  downloads=download_summary(session, rows),
                  EditionState=EditionState)


@router.post("/editions/{edition_id}/undo")
def edition_undo(edition_id: int, session: Session = Depends(get_session)):
    edition = session.get(Edition, edition_id)
    if edition is None:
        raise HTTPException(404, "no such edition")
    editions_mod.undo_delivery(session, edition)
    session.commit()
    return back("/editions", f"Edition {edition_id} marked unread again.")


@router.post("/editions/{edition_id}/deliver")
def edition_deliver(edition_id: int, session: Session = Depends(get_session)):
    edition = session.get(Edition, edition_id)
    if edition is None:
        raise HTTPException(404, "no such edition")
    editions_mod.mark_delivered(session, edition)
    session.commit()
    return back("/editions", f"Edition {edition_id} marked as read.")


@router.post("/editions/build")
def edition_build():
    scheduler.scheduler.add_job(jobs.run_build, id="manual-build",
                                replace_existing=True)
    return back("/editions", "Build queued.")


# --- jobs -----------------------------------------------------------------

@router.get("/jobs", response_class=HTMLResponse)
def job_list(request: Request, session: Session = Depends(get_session)):
    runs = list(session.execute(
        select(JobRun).order_by(JobRun.started_at.desc()).limit(120)
    ).scalars())
    scheduled = [
        {"id": j.id, "next": getattr(j, "next_run_time", None)}
        for j in scheduler.scheduler.get_jobs()
    ]
    return render(request, "jobs.html", runs=runs, scheduled=scheduled)


# --- OPDS access log ------------------------------------------------------

@router.get("/opds-log", response_class=HTMLResponse)
def opds_log(request: Request, kind: str = "",
             session: Session = Depends(get_session)):
    entries = access_log.recent(session, limit=200, kind=kind)
    sizes = dict(session.execute(
        select(Edition.id, Edition.size_bytes)).all())
    return render(request, "opds_log.html",
                  entries=entries, kind=kind,
                  clients=access_log.clients(session),
                  sizes=sizes, now=utcnow())


@router.get("/opds-log/data")
def opds_log_data(since_id: int = 0, kind: str = "",
                  session: Session = Depends(get_session)):
    """Rows newer than since_id, for the page's live refresh."""
    entries = access_log.recent(session, limit=200,
                                since_id=since_id or None, kind=kind)
    sizes = dict(session.execute(
        select(Edition.id, Edition.size_bytes)).all())

    def describe(e):
        total = sizes.get(e.edition_id) if e.edition_id else None
        pct = (round(100 * e.bytes_sent / total)
               if e.kind == "download" and total else None)
        return {
            "id": e.id,
            "ts": (e.ts.replace(tzinfo=timezone.utc) if e.ts.tzinfo is None
                   else e.ts).isoformat(),
            "method": e.method, "path": e.path, "status": e.status,
            "client_ip": e.client_ip or "", "user_agent": e.user_agent or "",
            "range": e.range_header or "", "bytes": e.bytes_sent,
            "ms": e.duration_ms, "kind": e.kind, "pct": pct,
        }

    return JSONResponse({
        "entries": [describe(e) for e in entries],
        "clients": [
            {"client_ip": c["client_ip"], "user_agent": c["user_agent"] or "",
             "requests": c["requests"], "downloads": c["downloads"],
             "errors": c["errors"], "bytes": c["bytes"],
             "last_seen": (c["last_seen"].replace(tzinfo=timezone.utc)
                           if c["last_seen"] and c["last_seen"].tzinfo is None
                           else c["last_seen"]).isoformat()
             if c["last_seen"] else None}
            for c in access_log.clients(session)
        ],
    })


@router.post("/opds-log/clear")
def opds_log_clear(session: Session = Depends(get_session)):
    removed = access_log.clear(session)
    session.commit()
    return back("/opds-log", f"Cleared {removed} log entries.")


# --- settings -------------------------------------------------------------

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, session: Session = Depends(get_session)):
    return render(request, "settings.html", groups=grouped(),
                  values=all_settings(session))


@router.post("/settings")
async def settings_save(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    prompt_warning = None
    for key in DEFAULTS:
        if key not in form:
            continue
        value = str(form[key]).strip()
        if key == "ai_clean_prompt" and "{chunk}" not in value:
            # Saving this would silently stop the article ever reaching the
            # model -- refuse just this field rather than corrupt cleanup.
            prompt_warning = ('The cleanup prompt was not saved: it must '
                              'contain the literal text {chunk} somewhere.')
            continue
        put(session, key, value)
    session.commit()
    scheduler.reload_all()
    timeutil.set_display_timezone(str(form.get("display_timezone", "UTC")))

    if prompt_warning:
        return back("/settings", "Other settings saved and schedule reloaded.",
                    err=prompt_warning)

    template = str(form.get("edition_title_format", "")).strip()
    if template and not editions_mod.title_is_unique_per_edition(template):
        return back("/settings", "Settings saved and schedule reloaded.",
                    err="Warning: the edition title has no {n}, {time} or "
                        "{datetime}, so every edition built on the same day "
                        "shares a title. Ereaders name the downloaded file "
                        "from that title, so each new edition will overwrite "
                        "the previous one on the device.")
    return back("/settings", "Settings saved and schedule reloaded.")


@router.get("/settings/ai/models")
def settings_ai_models(backend: str, base_url: str, api_key: str = ""):
    """Queried by the settings page's "Fetch available models" button.

    Takes the URL/key straight from the form rather than the saved settings,
    so the user can try a value before saving it.
    """
    if not base_url.strip():
        return JSONResponse({"error": "enter a base URL first"}, status_code=400)
    try:
        models = clean.list_models(backend, base_url.strip(), api_key=api_key)
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"could not reach the server: {exc}"},
                            status_code=502)
    except (ValueError, KeyError) as exc:
        return JSONResponse({"error": f"unexpected response: {exc}"},
                            status_code=502)
    return {"models": models}


@router.get("/settings/ai/test")
def settings_ai_test(backend: str, base_url: str, model: str, api_key: str = "",
                     timeout: int = 60):
    """Queried by the settings page's "Test model" button.

    Sends one trivial request through the real code path used for cleanup,
    so a wrong model name or an unreachable server shows up here instead of
    only being discovered on the next real article.
    """
    if not base_url.strip():
        return JSONResponse({"error": "enter a base URL first"}, status_code=400)
    if not model.strip():
        return JSONResponse({"error": "enter a model first"}, status_code=400)
    try:
        reply = clean.test_model(backend, base_url.strip(), model.strip(),
                                 api_key=api_key, timeout=timeout)
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"could not reach the server: {exc}"},
                            status_code=502)
    except (ValueError, KeyError, IndexError) as exc:
        return JSONResponse({"error": f"unexpected response: {exc}"},
                            status_code=502)
    return {"reply": reply}


# --- backup / restore -------------------------------------------------------

@router.get("/settings/backup")
def settings_backup():
    """Download a full backup: the database plus every EPUB/image/cover.

    Doubles as a migration file -- restoring it on another machine brings
    everything across, not just configuration.
    """
    from fastapi.responses import FileResponse
    from starlette.background import BackgroundTask

    try:
        path = backup.create_backup_archive()
    except Exception as exc:
        log.exception("backup failed")
        return back("/settings", err=f"Backup failed: {exc}")

    return FileResponse(
        path, media_type="application/gzip",
        filename=backup.backup_filename(),
        background=BackgroundTask(lambda: path.unlink(missing_ok=True)),
    )


@router.post("/settings/restore")
async def settings_restore(backup_file: UploadFile = File(...)):
    """Replace the live database and files with an uploaded backup.

    Validated before anything is touched. On success the process ends itself
    a moment after responding, so Docker's restart policy brings the app back
    up against the restored data -- see backup.schedule_restart for why that
    is simpler and safer than swapping the live database underneath the
    running scheduler and connection pool.
    """
    try:
        stage = backup.validate_backup_upload(backup_file.file)
    except backup.BackupError as exc:
        return back("/settings", err=f"Restore rejected: {exc}")
    except Exception as exc:
        log.exception("restore validation failed")
        return back("/settings", err=f"Restore failed: {exc}")

    try:
        # Only pause it if it is actually running -- restore has no business
        # failing outright just because the scheduler happens to be stopped
        # for some unrelated reason (polling disabled, or simply never
        # started, as under a test client with no lifespan). Restore does
        # not depend on the scheduler at all; this is only to avoid a job
        # firing in the narrow window between the swap and the restart.
        if scheduler.scheduler.running:
            scheduler.scheduler.pause()
        backup.apply_restore(stage)
    except Exception as exc:
        log.exception("restore failed while applying")
        return back("/settings", err=f"Restore failed while applying: {exc}")

    backup.schedule_restart()
    return back("/settings",
               "Restore applied. The server is restarting to load it -- "
               "reload this page in about 15 seconds.")
