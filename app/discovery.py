"""Background job discovery: poll tracked boards + wide sources, alert on matches.

One pass (``tick``) per user: gather postings from tracked boards *and* wide
discovery (RSS / ATS directory / SerpApi), drop ones we've already seen (dedupe —
so scoring tokens are spent once per posting, ever), free pre-filter, LLM/heuristic
score the survivors (capped per tick), persist, and notify per the configured
alert mode (digest / instant / silent). ``run_all`` sweeps every user with a
profile and/or tracked boards and is what the scheduler calls.

Delivery reuses ``reminders.get_sender()`` (Slack → Twilio → Log), so alerts
ride the exact same channel as reminders — no new transport.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from . import job_alerts, jobstore, matcher, profile, reminders, wide_discovery
from .config import get_settings
from .jobsources import NON_BOARD_SOURCES, JobPosting, fetch_source
from .jobsources import rss as rss_src

logger = logging.getLogger("discovery")


def _slug_variants(name: str) -> list[str]:
    """Plausible board tokens for a company name, most-likely first."""
    base = name.strip().lower()
    out = []
    for v in (re.sub(r"[^a-z0-9]", "", base),          # "acme inc" -> "acmeinc"
              re.sub(r"[^a-z0-9]+", "-", base).strip("-"),  # -> "acme-inc"
              base.split()[0] if base.split() else ""):     # -> "acme"
        if v and v not in out:
            out.append(v)
    return out


def resolve_board(company_name: str) -> dict | None:
    """Probe enabled free boards for one matching a company. All calls are free.

    Tries slug variants against each enabled source; returns the first that
    actually yields postings, or None if nothing public is found.
    """
    for source in get_settings().job_sources:
        if source in NON_BOARD_SOURCES:
            continue  # search/URL/cursor sources aren't per-company boards — never slug-probe
        for slug in _slug_variants(company_name):
            posts = fetch_source(source, slug)
            if posts:
                return {
                    "source": source,
                    "board_token": slug,
                    "company_name": company_name.strip().title(),
                    "count": len(posts),
                }
    return None


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


def resolve_feed(feed_id: str) -> dict | None:
    """Resolve an RSS feed id for ``track feed <id>``."""
    return rss_src.resolve_feed(feed_id)


def seed_board(user_id: str, source: str, board_token: str, company_name: str | None) -> int:
    """Record a board's CURRENT postings as already-seen (status 'seeded', no
    alerts, no scoring), so the user is only alerted on roles that appear AFTER
    they start tracking — not the entire existing backlog. Returns count seeded.

    Wide sources (rss, directory, aggregator) are not seeded — too noisy.
    """
    if wide_discovery.is_wide_source(source):
        return 0
    n = 0
    for p in fetch_source(source, board_token):
        if not p.external_id or jobstore.posting_exists(user_id, p.source, p.external_id):
            continue
        if company_name:
            p.company = company_name
        if jobstore.save_posting(user_id, p, relevance_score=None, status="seeded"):
            n += 1
    return n


def tick(user_id: str, *, sender=None, now: datetime | None = None) -> int:
    """Run one discovery pass for ``user_id``. Returns the number of messages sent."""
    prof = profile.get_profile(user_id)
    boards = jobstore.list_tracked(user_id)
    if not boards and not profile.has_profile(user_id):
        return 0
    settings = get_settings()

    # 0. Resurface any snoozed postings whose snooze has expired.
    jobstore.wake_snoozed(user_id, (now or datetime.now(timezone.utc)).isoformat())

    # 1. Tracked boards + wide discovery (RSS, directory, aggregator).
    fresh: list[JobPosting] = []
    seen: set[tuple[str, str]] = set()
    for b in boards:
        for p in fetch_source(b["source"], b["board_token"]):
            if not p.external_id:
                continue
            key = (p.source, p.external_id)
            if key in seen or jobstore.posting_exists(user_id, p.source, p.external_id):
                continue
            seen.add(key)
            if b["company_name"]:
                p.company = b["company_name"]
            fresh.append(p)
    fresh.extend(wide_discovery.collect_fresh(user_id, prof, existing_keys=seen))
    # Drop any wide items that raced into seen via posting_exists.
    fresh = [
        p for p in fresh
        if p.external_id
        and not jobstore.posting_exists(user_id, p.source, p.external_id)
    ]
    if not fresh:
        return 0

    # 2. Free pre-filter, then cap how many reach the (paid) scorer this tick.
    #    Postings beyond the cap aren't saved, so they're reconsidered next tick.
    candidates = matcher.prefilter(fresh, prof)[: settings.job_max_scored_per_tick]
    if not candidates:
        return 0

    # 3. Score (LLM when configured, else heuristic; never raises).
    scored = matcher.score(candidates, prof)

    # 4. Persist every scored posting (never re-scored) and notify per alert mode.
    #    Threshold is per-user (TUNE) falling back to the global default.
    sender = sender or reminders.get_sender()
    threshold = profile.effective_threshold(prof, settings.job_relevance_threshold)
    mode = settings.job_alert_mode_normalized
    notify_batch: list[tuple[JobPosting, float, int]] = []
    messages_sent = 0

    for posting, sc in scored:
        good = sc >= threshold
        if good:
            status = "alerted" if mode == "instant" else "queued"
        else:
            status = "new"
        row = jobstore.save_posting(
            user_id, posting, relevance_score=sc, status=status
        )
        if row is None or not good:
            continue
        notify_batch.append((posting, sc, row["id"]))

    if notify_batch and mode == "instant":
        for posting, sc, pid in notify_batch:
            try:
                sender.send(user_id, build_alert_body(posting, sc, pid))
                messages_sent += 1
            except Exception:  # noqa: BLE001 — one bad send never stops the batch
                logger.exception("alert send failed for posting %s", pid)
    elif notify_batch and mode == "digest":
        try:
            sender.send(user_id, job_alerts.build_digest(notify_batch))
            messages_sent = 1
        except Exception:  # noqa: BLE001
            logger.exception("digest send failed for %s", user_id)
    # silent: queued rows only, no outbound message

    if messages_sent:
        logger.info(
            "discovery: %d message(s), %d match(es) for %s (mode=%s)",
            messages_sent, len(notify_batch), user_id, mode,
        )
    return messages_sent


def run_all(*, sender=None) -> int:
    """One discovery pass per user with a profile and/or tracked boards."""
    global last_tick_at
    total = 0
    for user_id in jobstore.all_discovery_users():
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
