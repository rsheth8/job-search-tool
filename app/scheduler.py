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

from . import reminders

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
    sched.start()
    _scheduler = sched
    logger.info("reminder scheduler started (every %ds)", POLL_SECONDS)
    return sched


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sent = tick()
    print(f"Delivered {sent} due reminder(s).")
