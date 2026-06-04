"""Background job discovery: poll tracked boards, alert on good new matches.

One pass (``tick``) per user: fetch every tracked board, drop postings we've
already seen (dedupe — so scoring tokens are spent once per posting, ever),
free pre-filter, LLM/heuristic score the survivors (capped per tick), persist,
and Slack-alert the ones above the relevance threshold. ``run_all`` sweeps every
user with tracked boards and is what the scheduler calls.

Delivery reuses ``reminders.get_sender()`` (Slack → Twilio → Log), so alerts
ride the exact same channel as reminders — no new transport.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import jobstore, matcher, profile, reminders
from .config import get_settings
from .jobsources import JobPosting, fetch_source

logger = logging.getLogger("discovery")

# Set on each run_all() so /health can show liveness without a separate store.
last_tick_at: str | None = None


def build_alert_body(posting: JobPosting, score: float, posting_id: int) -> str:
    lines = [
        f"🆕 {posting.title or 'New role'} @ {posting.company or posting.source}",
    ]
    if posting.location:
        lines.append(f"📍 {posting.location}")
    if posting.url:
        lines.append(f"🔗 {posting.url}")
    lines.append(f"(match {round(score * 100)}% · #{posting_id} — reply “apply {posting_id}” to log it)")
    return "\n".join(lines)


def tick(user_id: str, *, sender=None, now: datetime | None = None) -> int:
    """Run one discovery pass for ``user_id``. Returns the number of alerts sent."""
    boards = jobstore.list_tracked(user_id)
    if not boards:
        return 0
    prof = profile.get_profile(user_id)
    settings = get_settings()

    # 1. Fetch every board, keep only postings we haven't recorded before.
    fresh: list[JobPosting] = []
    for b in boards:
        for p in fetch_source(b["source"], b["board_token"]):
            if not p.external_id or jobstore.posting_exists(user_id, p.source, p.external_id):
                continue
            if b["company_name"]:
                p.company = b["company_name"]  # prefer the tracked display name
            fresh.append(p)
    if not fresh:
        return 0

    # 2. Free pre-filter, then cap how many reach the (paid) scorer this tick.
    #    Postings beyond the cap aren't saved, so they're reconsidered next tick.
    candidates = matcher.prefilter(fresh, prof)[: settings.job_max_scored_per_tick]
    if not candidates:
        return 0

    # 3. Score (LLM when configured, else heuristic; never raises).
    scored = matcher.score(candidates, prof)

    # 4. Persist every scored posting (so it's never re-scored) and alert the
    #    ones above threshold.
    sender = sender or reminders.get_sender()
    threshold = settings.job_relevance_threshold
    alerts = 0
    for posting, sc in scored:
        good = sc >= threshold
        row = jobstore.save_posting(
            user_id, posting, relevance_score=sc, status="alerted" if good else "new"
        )
        if row is None or not good:
            continue
        try:
            sender.send(user_id, build_alert_body(posting, sc, row["id"]))
            alerts += 1
        except Exception:  # noqa: BLE001 — one bad send never stops the batch
            logger.exception("alert send failed for posting %s", row["id"])
    if alerts:
        logger.info("discovery: %d new alert(s) for %s", alerts, user_id)
    return alerts


def run_all(*, sender=None) -> int:
    """One discovery pass for every user with tracked boards. Returns total alerts."""
    global last_tick_at
    total = 0
    for user_id in jobstore.all_tracked_users():
        try:
            total += tick(user_id, sender=sender)
        except Exception:  # noqa: BLE001 — one user's failure never stops the sweep
            logger.exception("discovery tick failed for %s", user_id)
    last_tick_at = datetime.now(timezone.utc).isoformat()
    return total


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from .db import init_db  # self-sufficient when run as a one-shot (e.g. cron)

    init_db()
    sent = run_all()
    print(f"Discovery sent {sent} new alert(s).")
