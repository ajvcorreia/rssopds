"""Access logging for the OPDS endpoints.

This is a pure-ASGI middleware rather than a Starlette BaseHTTPMiddleware on
purpose: wrapping `send` counts the bytes of a *streamed* response, which
BaseHTTPMiddleware would buffer away.

What the byte count actually means: bytes handed to the transport, not bytes
the client acknowledged. HTTP gives the server no delivery receipt, so this is
the closest thing available. For a response small enough to fit in the kernel
socket buffer (roughly a few hundred KB) the whole body is accepted at once,
so a reader that connects and immediately dies still reads as a full transfer.
Above that size asyncio flow control makes `send` wait for the socket to
drain, so the count tracks real progress and a mid-download abort shows up
short. Editions with images are comfortably in that range; a tiny text-only
one is not.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import timedelta

from sqlalchemy import case, delete, func, select
from starlette.concurrency import run_in_threadpool

from ..db import session_scope
from ..models import OpdsAccess, utcnow
from ..settings_store import get as setting

log = logging.getLogger(__name__)

# Both /opds/edition/4.epub and /opds/edition/4/technology-no004-....epub
EDITION_RE = re.compile(r"^/opds/edition/(\d+)(?:/[^/]*)?\.epub$")
COVER_RE = re.compile(r"^/opds/cover/(\d+)\.jpg$")


def _classify(path: str) -> tuple[str, int | None]:
    match = EDITION_RE.match(path)
    if match:
        return "download", int(match.group(1))
    match = COVER_RE.match(path)
    if match:
        return "cover", int(match.group(1))
    stripped = path.rstrip("/")
    # ebooks-file must be checked before the ebooks nav prefix -- textually
    # "/opds/ebooks-file/x" already starts with "/opds/ebooks".
    if stripped.startswith("/opds/ebooks-file/"):
        return "download", None
    if (stripped in ("/opds", "/opds/rss") or stripped == "/opds/ebooks"
            or stripped.startswith("/opds/ebooks/")):
        return "catalog", None
    return "other", None


def _client_ip(scope) -> str:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers", [])}
    forwarded = headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    client = scope.get("client")
    return (client[0] if client else "")[:64]


def _header(scope, name: str) -> str:
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == target:
            return value.decode("latin-1", "replace")
    return ""


def _write(entry: dict) -> None:
    try:
        with session_scope() as session:
            session.add(OpdsAccess(**entry))
            _prune(session)
    except Exception:
        # Logging must never take the server down.
        log.exception("could not write OPDS access log entry")


def _prune(session) -> None:
    keep = setting(session, "opds_log_keep")
    total = session.execute(
        select(func.count(OpdsAccess.id))).scalar_one()
    if total > keep * 1.2:  # prune in batches, not on every single request
        cutoff = session.execute(
            select(OpdsAccess.id).order_by(OpdsAccess.id.desc())
            .offset(keep).limit(1)
        ).scalar_one_or_none()
        if cutoff is not None:
            session.execute(delete(OpdsAccess).where(OpdsAccess.id <= cutoff))


class OpdsAccessLogMiddleware:
    def __init__(self, app, prefix: str = "/opds"):
        self.app = app
        self.prefix = prefix

    def _in_scope(self, path: str) -> bool:
        # Exact match or a real sub-path only. A plain startswith() would also
        # catch the web UI's own /opds-log pages, so the live-refresh poller
        # would fill the log with its own requests every few seconds.
        return path == self.prefix or path.startswith(self.prefix + "/")

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self._in_scope(
                scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        state = {"status": 0, "bytes": 0, "disconnected": False}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
            elif message["type"] == "http.response.body":
                state["bytes"] += len(message.get("body", b"") or b"")
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            state["disconnected"] = True
            raise
        finally:
            path = scope.get("path", "")
            kind, edition_id = _classify(path)
            entry = {
                "ts": utcnow(),
                "method": scope.get("method", "GET")[:8],
                "path": path[:512],
                "query": (scope.get("query_string", b"").decode(
                    "latin-1", "replace") or None),
                "status": state["status"],
                "client_ip": _client_ip(scope),
                "user_agent": _header(scope, "user-agent")[:255] or None,
                "range_header": _header(scope, "range")[:128] or None,
                "bytes_sent": state["bytes"],
                "duration_ms": int((time.monotonic() - started) * 1000),
                "edition_id": edition_id,
                "kind": kind,
                "disconnected": state["disconnected"],
            }
            await run_in_threadpool(_write, entry)


# --- queries used by the web UI ------------------------------------------

def recent(session, limit: int = 200, since_id: int | None = None,
           kind: str = "") -> list[OpdsAccess]:
    query = select(OpdsAccess)
    if since_id:
        query = query.where(OpdsAccess.id > since_id)
    if kind == "errors":
        query = query.where(OpdsAccess.status >= 400)
    elif kind:
        query = query.where(OpdsAccess.kind == kind)
    query = query.order_by(OpdsAccess.id.desc()).limit(limit)
    return list(session.execute(query).scalars())


def clients(session, hours: int = 168) -> list[dict]:
    """Distinct devices seen recently -- the "is my reader talking to it?" view."""
    cutoff = utcnow() - timedelta(hours=hours)
    rows = session.execute(
        select(
            OpdsAccess.client_ip,
            OpdsAccess.user_agent,
            func.count(OpdsAccess.id),
            func.max(OpdsAccess.ts),
            func.sum(OpdsAccess.bytes_sent),
            func.sum(case((OpdsAccess.status >= 400, 1), else_=0)),
            func.sum(case((OpdsAccess.kind == "download", 1), else_=0)),
        )
        .where(OpdsAccess.ts >= cutoff)
        .group_by(OpdsAccess.client_ip, OpdsAccess.user_agent)
        .order_by(func.max(OpdsAccess.ts).desc())
    ).all()
    return [
        {"client_ip": ip, "user_agent": ua, "requests": n, "last_seen": last,
         "bytes": total or 0, "errors": errs or 0, "downloads": dls or 0}
        for ip, ua, n, last, total, errs, dls in rows
    ]


def clear(session) -> int:
    total = session.execute(select(func.count(OpdsAccess.id))).scalar_one()
    session.execute(delete(OpdsAccess))
    return total
