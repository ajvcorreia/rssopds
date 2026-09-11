"""OPDS endpoints, including the download tracking that drives mark-as-read."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ebooks
from ..config import config
from ..db import get_session, session_scope
from ..models import (
    Article, Category, Delivery, Edition, EditionArticle, EditionState,
)
from ..pipeline import covers, editions as editions_mod
from ..security import require_opds
from .. import timeutil
from ..settings_store import get as setting
from . import feed as feedgen

log = logging.getLogger(__name__)

router = APIRouter(tags=["opds"], dependencies=[Depends(require_opds)])

CHUNK = 64 * 1024
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def base_url(request: Request) -> str:
    if config.base_url:
        return config.base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def _resolve_ebook_path(subpath: str) -> Path:
    target = ebooks.resolve(subpath)
    if target is None:
        raise HTTPException(404, "not found")
    return target


@router.get("/opds")
def catalog_root(request: Request, session: Session = Depends(get_session)) -> Response:
    xml = feedgen.opds_root(
        base=base_url(request),
        title=setting(session, "catalog_title"),
        publisher=str(setting(session, "publisher_name")).strip(),
    )
    return Response(content=xml, media_type=feedgen.OPDS_NAVIGATION)


@router.get("/opds/rss")
def catalog(request: Request, session: Session = Depends(get_session)) -> Response:
    entries = editions_mod.catalog_categories(session)

    titles: dict[int, list[str]] = {}
    for _category, edition in entries:
        titles[edition.id] = [
            t for (t,) in session.execute(
                select(Article.title)
                .join(EditionArticle, EditionArticle.article_id == Article.id)
                .where(EditionArticle.edition_id == edition.id)
                .order_by(EditionArticle.position)
            )
        ]

    xml = feedgen.rss_catalog(
        title=setting(session, "catalog_title"),
        base=base_url(request),
        entries=entries,
        article_titles=titles,
        publisher=str(setting(session, "publisher_name")).strip(),
    )
    return Response(content=xml, media_type=feedgen.OPDS_ACQUISITION)


@router.get("/opds/ebooks")
@router.get("/opds/ebooks/{subpath:path}")
def ebooks_browse(request: Request, subpath: str = "") -> Response:
    target = _resolve_ebook_path(subpath)
    if not target.is_dir():
        raise HTTPException(404, "not found")

    dirs, files = ebooks.list_dir(target)

    xml = feedgen.ebooks_nav(base=base_url(request), path=subpath.strip("/"),
                             dirs=dirs, files=files)
    return Response(content=xml, media_type=feedgen.OPDS_NAVIGATION)


@router.get("/opds/ebooks-file/{subpath:path}")
def ebooks_download(subpath: str) -> Response:
    target = _resolve_ebook_path(subpath)
    if not target.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(target, media_type=ebooks.guess_mime(target), filename=target.name)


@router.get("/opds/cover/{category_id}.jpg")
def cover(category_id: int, session: Session = Depends(get_session)) -> Response:
    category = session.get(Category, category_id)
    if category is None:
        raise HTTPException(404, "no such category")

    path = covers.resolve(category, config.cover_dir, when=timeutil.now(),
                          width=setting(session, "image_max_width"),
                          height=setting(session, "image_max_height"))
    return FileResponse(path, media_type="image/jpeg")


def _delivery_for(session: Session, edition: Edition, request: Request) -> Delivery:
    """One Delivery row per (edition, client), so ranged fetches accumulate."""
    ua = (request.headers.get("user-agent") or "")[:255]
    ip = request.client.host if request.client else ""
    delivery = session.execute(
        select(Delivery).where(Delivery.edition_id == edition.id,
                               Delivery.user_agent == ua,
                               Delivery.client_ip == ip)
        .order_by(Delivery.id.desc())
    ).scalars().first()
    if delivery is None:
        delivery = Delivery(edition_id=edition.id, user_agent=ua, client_ip=ip)
        session.add(delivery)
        session.flush()
    return delivery


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Return [start, end) for a single-range request, or None."""
    if not header:
        return None
    match = RANGE_RE.fullmatch(header.strip())
    if not match:
        return None
    raw_start, raw_end = match.groups()
    if raw_start == "" and raw_end == "":
        return None
    if raw_start == "":  # suffix range: last N bytes
        length = min(int(raw_end), size)
        return size - length, size
    start = int(raw_start)
    end = min(int(raw_end) + 1, size) if raw_end else size
    if start >= size or start >= end:
        return None
    return start, end


@router.get("/opds/edition/{edition_id}/{filename}.epub")
def download_named(edition_id: int, filename: str, request: Request,
                   session: Session = Depends(get_session)) -> Response:
    """Download under the edition's own filename.

    The catalogue advertises this form so that a reader naming the saved file
    from the URL gets a distinct name per edition. `filename` is decorative --
    edition_id alone identifies the book.
    """
    return download(edition_id, request, session)


@router.get("/opds/edition/{edition_id}.epub")
def download(edition_id: int, request: Request,
             session: Session = Depends(get_session)) -> Response:
    edition = session.get(Edition, edition_id)
    if edition is None or not edition.epub_file:
        raise HTTPException(404, "no such edition")

    path: Path = config.epub_dir / edition.epub_file
    if not path.exists():
        raise HTTPException(410, "edition file has been pruned")

    size = path.stat().st_size
    span = _parse_range(request.headers.get("range"), size)
    start, end = span if span else (0, size)

    delivery_id = _delivery_for(session, edition, request).id
    session.commit()

    def stream():
        # Bytes are counted as they are handed to the transport, then folded
        # into the delivery's coverage map -- including on an early
        # disconnect, where only the part that actually got out is credited.
        sent = 0
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                remaining = end - start
                while remaining > 0:
                    block = fh.read(min(CHUNK, remaining))
                    if not block:
                        break
                    remaining -= len(block)
                    sent += len(block)
                    yield block
        finally:
            if sent > 0:
                try:
                    with session_scope() as s:
                        delivery = s.get(Delivery, delivery_id)
                        ed = s.get(Edition, edition_id)
                        if delivery is not None and ed is not None:
                            done = editions_mod.record_bytes(
                                s, delivery, start, start + sent, size)
                            if done:
                                log.info("edition %s fully transferred to %s",
                                         edition_id, delivery.client_ip)
                except Exception:
                    log.exception("could not record delivery bytes")

    headers = {
        "Content-Length": str(end - start),
        "Accept-Ranges": "bytes",
        "Content-Disposition":
            f'attachment; filename="{edition.epub_file}"',
    }
    status = 200
    if span:
        headers["Content-Range"] = f"bytes {start}-{end - 1}/{size}"
        status = 206

    return StreamingResponse(stream(), status_code=status,
                             media_type="application/epub+zip", headers=headers)


@router.head("/opds/edition/{edition_id}.epub")
def download_head(edition_id: int,
                  session: Session = Depends(get_session)) -> Response:
    """Some readers HEAD before GET; a HEAD must never count as a delivery."""
    edition = session.get(Edition, edition_id)
    if edition is None or not edition.epub_file:
        raise HTTPException(404, "no such edition")
    path = config.epub_dir / edition.epub_file
    if not path.exists():
        raise HTTPException(410, "edition file has been pruned")
    return Response(status_code=200, media_type="application/epub+zip",
                    headers={"Content-Length": str(path.stat().st_size),
                             "Accept-Ranges": "bytes"})
