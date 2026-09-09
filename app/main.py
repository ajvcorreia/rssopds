"""Application entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import scheduler
from .config import config
from .db import init_db, session_scope
from .opds.access_log import OpdsAccessLogMiddleware
from .opds.routes import router as opds_router
from .web.routes import router as web_router

logging.basicConfig(
    level=getattr(logging, config.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("rssopds")


def _warn_about_legacy_env() -> None:
    """Shout if a .env still uses the old, misspelled variable prefix.

    Those names are no longer read, so a stale file would silently switch
    authentication *off* -- the one misconfiguration that must never pass
    quietly.
    """
    import os

    stale = sorted(k for k in os.environ if k.startswith("RSSOSPD_"))
    if not stale:
        return
    log.error("Ignoring %d variable(s) using the old RSSOSPD_ prefix: %s. "
              "Rename them to RSSOPDS_ -- until you do, these settings "
              "(including any credentials) are NOT in effect.",
              len(stale), ", ".join(stale))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _warn_about_legacy_env()
    init_db()

    from .settings_store import get as _get
    from . import timeutil
    with session_scope() as _s:
        applied = timeutil.set_display_timezone(_get(_s, "display_timezone"))
    log.info("display timezone: %s", applied)

    # Progressive JPEGs are invisible on Adobe RMSDK readers. Cached images are
    # never re-downloaded, and built EPUBs embed a copy, so fixing the encoder
    # alone would leave existing books broken -- repair the cache, then force
    # any undelivered edition to be rebuilt from it.
    from .config import config as cfg
    from .pipeline import editions as editions_mod
    from .pipeline import images as images_mod

    repaired = images_mod.repair_progressive(cfg.image_dir)

    # Same reasoning for single-component greyscale JPEGs, which many small
    # readers cannot decode. Only convert when greyscale is actually switched
    # off, so anyone who deliberately wants it keeps it.
    from .settings_store import get as _setting
    with session_scope() as session:
        greyscale_wanted = bool(_setting(session, "image_grayscale"))
    if not greyscale_wanted:
        repaired += images_mod.repair_greyscale(cfg.image_dir)
        repaired += images_mod.repair_greyscale(cfg.cover_dir)

    # Cached images are never re-fetched, so lowering the screen size would
    # otherwise only affect articles that have yet to arrive.
    with session_scope() as session:
        box = (_setting(session, "image_max_width"),
               _setting(session, "image_max_height"))
    repaired += images_mod.repair_oversized(cfg.image_dir, *box)

    if repaired:
        with session_scope() as session:
            editions_mod.supersede_available(session)

    scheduler.start()
    log.info("RSSOPDS ready on %s:%s", config.host, config.port)
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="RSSOPDS", docs_url=None, redoc_url=None,
              lifespan=lifespan)

app.add_middleware(OpdsAccessLogMiddleware)

app.include_router(opds_router)
app.include_router(web_router)


@app.get("/healthz")
def healthz() -> JSONResponse:
    from sqlalchemy import select

    from .models import Feed
    try:
        with session_scope() as session:
            feeds = len(session.execute(select(Feed.id)).all())
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    return JSONResponse({
        "ok": True,
        "feeds": feeds,
        "scheduler": scheduler.scheduler.running,
        "jobs": len(scheduler.scheduler.get_jobs()),
    })


def main() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host=config.host, port=config.port,
                log_level=config.log_level.lower())


if __name__ == "__main__":
    main()
