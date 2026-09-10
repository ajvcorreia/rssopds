"""Web UI. Every configuration option in this app is set from here."""
from __future__ import annotations

import logging
import re
import shutil
from datetime import timedelta, timezone
from pathlib import Path

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Request, UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import jobs, scheduler
from ..config import config
from ..db import get_session
from ..models import (
    Article, ArticleState, Category, Edition, EditionState, Feed, FeedItem,
    JobRun, SourceKind, ThreadMode, utcnow,
)
from ..opds import access_log
from ..pipeline import covers, editions as editions_mod
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

    catalog = editions_mod.catalog_categories(session)
    feeds = list(session.execute(select(Feed).order_by(Feed.title)).scalars())
    recent = list(session.execute(
        select(JobRun).order_by(JobRun.started_at.desc()).limit(12)
    ).scalars())

    return render(request, "dashboard.html",
                  counts=state_counts, held=held, catalog=catalog,
                  feeds=feeds, recent=recent, ArticleState=ArticleState,
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
        select(Edition).order_by(Edition.created_at.desc()).limit(60)
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
    for key in DEFAULTS:
        if key in form:
            put(session, key, str(form[key]).strip())
    session.commit()
    scheduler.reload_all()
    timeutil.set_display_timezone(str(form.get("display_timezone", "UTC")))

    template = str(form.get("edition_title_format", "")).strip()
    if template and not editions_mod.title_is_unique_per_edition(template):
        return back("/settings", "Settings saved and schedule reloaded.",
                    err="Warning: the edition title has no {n}, {time} or "
                        "{datetime}, so every edition built on the same day "
                        "shares a title. Ereaders name the downloaded file "
                        "from that title, so each new edition will overwrite "
                        "the previous one on the device.")
    return back("/settings", "Settings saved and schedule reloaded.")
