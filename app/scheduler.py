"""APScheduler wrapper around the reminder delivery loop.

This is intentionally thin: it owns *when* to run, not *what* to do. The work
lives in ``reminders.deliver_due_reminders`` (pure, testable, no apscheduler
import). Keeping the dependency isolated here means the core ships and tests
even if apscheduler isn't installed.

Run modes:
  * In-process: ``start_scheduler()`` from app startup → polls every minute.
  * One-shot:   ``python -m app.scheduler`` → deliver due reminders once and
    exit (handy for cron, or manual testing without a long-running process).
"""
from __future__ import annotations

import logging

from . import discovery, reminders
from .config import get_settings

logger = logging.getLogger("scheduler")

POLL_SECONDS = 60

_scheduler = None  # module-level singleton so we don't double-start


def is_running() -> bool:
    return _scheduler is not None and getattr(_scheduler, "running", False)


def tick() -> int:
    """Deliver any due reminders once. Returns the number sent."""
    n = reminders.deliver_due_reminders()
    if n:
        logger.info("delivered %d due reminder(s)", n)
    return n


def discovery_tick() -> int:
    """Poll tracked job boards once and alert on good new matches."""
    n = discovery.run_all()
    if n:
        logger.info("discovery sent %d new alert(s)", n)
    return n


def start_scheduler():
    """Start the background poll loop. No-op if already running or unavailable."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        logger.warning(
            "apscheduler not installed — reminder loop disabled. "
            "Run `python -m app.scheduler` via cron, or pip install apscheduler."
        )
        return None
    sched = BackgroundScheduler(daemon=True)
    sched.add_job(tick, "interval", seconds=POLL_SECONDS, id="reminder_tick")
    job_poll = get_settings().job_poll_seconds
    sched.add_job(discovery_tick, "interval", seconds=job_poll, id="discovery_tick")
    sched.start()
    _scheduler = sched
    logger.info(
        "scheduler started (reminders every %ds, discovery every %ds)",
        POLL_SECONDS, job_poll,
    )
    return sched


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sent = tick()
    print(f"Delivered {sent} due reminder(s).")
