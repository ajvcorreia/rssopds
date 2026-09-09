"""APScheduler wiring.

Feeds get one job each at their own interval; the assemble/process/build stages
run on global timers. Everything is rescheduled when the user changes settings
or edits a feed, so nothing needs a restart.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from . import jobs
from .db import session_scope
from .models import Feed
from .settings_store import get as setting

log = logging.getLogger(__name__)

scheduler = BackgroundScheduler(
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
    timezone="UTC",
)


def _feed_job_id(feed_id: int) -> str:
    return f"poll-feed-{feed_id}"


def reschedule_feed(feed: Feed) -> None:
    job_id = _feed_job_id(feed.id)
    if not feed.enabled:
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        return
    minutes = max(1, feed.poll_interval_min)
    scheduler.add_job(
        jobs.run_poll, IntervalTrigger(minutes=minutes),
        id=job_id, args=[feed.id], replace_existing=True,
        # Stagger start so twenty feeds do not all fire the moment we boot.
        jitter=min(60, minutes * 6),
    )


def remove_feed(feed_id: int) -> None:
    job_id = _feed_job_id(feed_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def reload_all() -> None:
    """Rebuild the whole schedule from the current database state."""
    with session_scope() as session:
        polling = bool(setting(session, "poll_enabled"))
        assemble_min = max(1, setting(session, "assemble_interval_min"))
        process_min = max(1, setting(session, "process_interval_min"))
        build_min = max(1, setting(session, "build_interval_min"))
        feeds = list(session.execute(select(Feed)).scalars())

        for job in scheduler.get_jobs():
            if job.id.startswith("poll-feed-"):
                scheduler.remove_job(job.id)

        if polling:
            for feed in feeds:
                reschedule_feed(feed)

    scheduler.add_job(jobs.run_assemble, IntervalTrigger(minutes=assemble_min),
                      id="assemble", replace_existing=True)
    scheduler.add_job(jobs.run_process, IntervalTrigger(minutes=process_min),
                      id="process", replace_existing=True)
    scheduler.add_job(jobs.run_build, IntervalTrigger(minutes=build_min),
                      id="build", replace_existing=True)
    log.info("scheduler reloaded: %d jobs", len(scheduler.get_jobs()))


def start() -> None:
    if not scheduler.running:
        scheduler.start()
    reload_all()


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
