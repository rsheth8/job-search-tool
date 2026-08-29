"""Background job discovery: poll tracked boards + wide sources, alert on matches.

One pass (``tick``) per user: gather postings from tracked boards *and* wide
discovery (RSS / ATS directory / swelist / YC), drop ones we've already seen
(dedupe — so scoring tokens are spent once per posting, ever), free pre-filter,
LLM/heuristic score the survivors (capped per tick), persist, and notify per the
configured alert mode (digest / instant / silent). ``run_all`` sweeps every
user with a profile and/or tracked boards and is what the scheduler calls.

Delivery reuses ``reminders.get_sender()`` (AppSender), so alerts ride the same
in-app channel as reminders — no separate transport.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

from . import (
    eligibility, job_alerts, jobstore, matcher, posting_match, profile,
    reminders, reranker, wide_discovery,
)
from .config import get_settings
from .jobsources import NON_BOARD_SOURCES, JobPosting, fetch_source
from .jobsources import ghost, quality
from .jobsources import rss as rss_src

logger = logging.getLogger("discovery")


def _slug_variants(name: str) -> list[str]:
    """Plausible board tokens for a company name, most-likely first."""
    raw = name.strip()
    base = raw.lower()
    out = []
    for v in (re.sub(r"[^a-z0-9]", "", base),          # "acme inc" -> "acmeinc"
              re.sub(r"[^a-z0-9]+", "-", base).strip("-"),  # -> "acme-inc"
              base.split()[0] if base.split() else "",     # -> "acme"
              re.sub(r"[^A-Za-z0-9]", "", raw)):           # "Service Now" -> "ServiceNow"
        if v and v not in out:
            out.append(v)
    return out


def resolve_board(company_name: str) -> dict | None:
    """Probe enabled free boards for one matching a company. All calls are free.

    Tries the sector catalog first (known ATS token), then slug variants against
    each enabled source. Returns the first that actually yields postings, or None.
    """
    from . import catalog

    known = catalog.lookup_board(company_name)
    if known and known["source"] in get_settings().job_sources:
        posts = fetch_source(known["source"], known["board_token"])
        if posts:
            return {
                "source": known["source"],
                "board_token": known["board_token"],
                "company_name": known["company_name"],
                "count": len(posts),
            }
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


def _deliver_chat(sender, user_id: str, body: str) -> None:
    """Transcript only. Match push is ``notify_new_matches`` so the banner is
    one personal alert that opens Apply, not a second generic 'Apply' ping."""
    try:
        sender.send(user_id, body, push_alert=False)
    except TypeError:
        sender.send(user_id, body)


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

    Wide sources (rss, directory, swelist) are not seeded — too noisy.
    """
    if wide_discovery.is_wide_source(source):
        return 0
    n = 0
    for p in fetch_source(source, board_token):
        if not p.external_id or jobstore.posting_exists(user_id, p.source, p.external_id):
            continue
        if company_name:
            p.company = company_name
        p.external_id = posting_match.normalize_external_id(
            p.source, board_token, p.external_id
        )
        if jobstore.save_posting(user_id, p, relevance_score=None, status="seeded"):
            n += 1
    return n


def tick(user_id: str, *, sender=None, now: datetime | None = None) -> int:
    """Run one discovery pass for ``user_id``. Returns the number of messages sent."""
    from . import llm_budget

    with llm_budget.for_user(user_id):
        return _tick(user_id, sender=sender, now=now)


def _tick(user_id: str, *, sender=None, now: datetime | None = None) -> int:
    """Run one discovery pass for ``user_id``. Returns the number of messages sent."""
    prof = profile.get_profile(user_id)
    boards = jobstore.list_tracked(user_id)
    if not boards and not profile.has_profile(user_id):
        return 0
    settings = get_settings()

    # 0. Resurface any snoozed postings whose snooze has expired.
    jobstore.wake_snoozed(user_id, (now or datetime.now(timezone.utc)).isoformat())

    # 0b. Grow the rotating directory from catalog names in this user's sector
    #     so this tick's directory pass can pick up newly learned boards.
    if settings.job_catalog_probe_enabled:
        try:
            from . import catalog_probe

            catalog_probe.probe_for_user(user_id, prof)
        except Exception:  # noqa: BLE001 — probing must never kill the tick
            logger.exception("catalog probe failed for %s", user_id)

    # 1. Tracked boards + wide discovery (RSS, directory, swelist, YC).
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
            p.external_id = posting_match.normalize_external_id(
                p.source, b["board_token"], p.external_id
            )
            fresh.append(p)
    fresh.extend(wide_discovery.collect_fresh(user_id, prof, existing_keys=seen))
    # Drop any wide items that raced into seen via posting_exists.
    fresh = [
        p for p in fresh
        if p.external_id
        and not jobstore.posting_exists(user_id, p.source, p.external_id)
    ]
    # Reputability gate: keep first-party ATS results; drop placeholder/spam from
    # RSS / directory feeds before spending scoring tokens on them.
    fresh, dropped = quality.filter_reputable(fresh)
    if dropped:
        logger.info("discovery: dropped %d low-reputation posting(s) for %s", dropped, user_id)
    # Cross-source dedupe: the same job reaches us from the ATS, an RSS feed, and
    # multiple boards at once. Source-level dedupe can't see that (different ids),
    # so it showed up three times. Keeps the first-party copy — apply direct.
    fresh, duped = quality.dedupe(fresh)
    if duped:
        logger.info("discovery: collapsed %d duplicate posting(s) for %s", duped, user_id)
    # Ghost-job gate: drop never-really-hiring reqs (evergreen/reposted/stale/scam).
    if settings.ghost_filter_enabled and fresh:
        kept: list[JobPosting] = []
        ghosted = 0
        for p in fresh:
            reposts = jobstore.seen_similar_count(user_id, p.company, p.title)
            if ghost.is_ghost(p, repost_count=reposts):
                ghosted += 1
                continue
            kept.append(p)
        if ghosted:
            logger.info("discovery: dropped %d ghost posting(s) for %s", ghosted, user_id)
        fresh = kept
    # Eligibility gate (rule tier): drop roles the candidate clearly isn't
    # qualified for / couldn't realistically do, given their level.
    if settings.eligibility_filter_enabled and fresh:
        fresh, unfit = eligibility.filter_eligible(fresh, prof)
        if unfit:
            logger.info("discovery: dropped %d over-qualified posting(s) for %s", unfit, user_id)
    if not fresh:
        return 0

    # 2. Free pre-filter, then cap how many reach the (paid) scorer this tick.
    #    Fillable + fresh first so today's ATS drops beat a rotating RSS backlog.
    #    Postings beyond the cap aren't saved, so they're reconsidered next tick.
    from . import shortlist

    candidates = matcher.prefilter(fresh, prof)
    candidates.sort(key=lambda p: shortlist.discovery_sort_key(p), reverse=True)
    candidates = candidates[: settings.job_max_scored_per_tick]
    if settings.job_verify_apply_urls and candidates:
        from .jobsources import alive

        candidates, dead = alive.filter_open(candidates)
        if dead:
            logger.info("discovery: dropped %d closed apply URL(s) for %s", dead, user_id)
    if not candidates:
        return 0

    # 3. Score (LLM/heuristic; never raises), then personalize the
    #    ranking with the user's own apply/dismiss model (no-op until trained).
    scored = matcher.score(candidates, prof)
    if settings.reranker_enabled:
        reranker.maybe_retrain(user_id, prof)
        scored = reranker.rerank(user_id, prof, scored)

    # 4. Persist every scored posting (never re-scored) and notify per alert mode.
    #    Threshold is per-user (TUNE) falling back to the global default.
    sender = sender or reminders.get_sender()
    threshold = profile.effective_threshold(prof, settings.job_relevance_threshold)
    mode = settings.job_alert_mode_normalized
    auto_threshold = settings.job_auto_queue_threshold
    notify_batch: list[tuple[JobPosting, float, int]] = []
    auto_stage_ids: list[int] = []
    messages_sent = 0

    for posting, sc in scored:
        if posting_match.user_already_applied_to(
            user_id, posting.company, posting.title
        ):
            jobstore.save_posting(
                user_id, posting, relevance_score=sc, status="applied"
            )
            continue
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
        # Auto-stage the strongest matches into the apply queue (skip triage).
        if auto_threshold > 0 and sc >= auto_threshold:
            auto_stage_ids.append(row["id"])

    auto_staged = 0
    if auto_stage_ids:
        from . import apply_queue
        for pid in auto_stage_ids:
            try:
                if apply_queue.stage(user_id, pid):
                    auto_staged += 1
            except Exception:  # noqa: BLE001 — staging is best-effort
                logger.exception("auto-stage failed for posting %s", pid)

    auto_note = (
        f"\n\n📥 {auto_staged} top match(es) auto-staged — review & apply at /apply"
        if auto_staged else ""
    )

    if notify_batch and mode == "instant":
        for posting, sc, pid in notify_batch:
            try:
                _deliver_chat(sender, user_id, build_alert_body(posting, sc, pid))
                messages_sent += 1
            except Exception:  # noqa: BLE001 — one bad send never stops the batch
                logger.exception("alert send failed for posting %s", pid)
    elif notify_batch and mode == "digest":
        try:
            _deliver_chat(
                sender, user_id,
                job_alerts.build_digest(notify_batch, user_id=user_id) + auto_note,
            )
            messages_sent = 1
        except Exception:  # noqa: BLE001
            logger.exception("digest send failed for %s", user_id)
    # silent: queued rows only, no outbound message

    # A push alongside the chat message, so the phone surfaces new matches without
    # Fail-open and no-op until push is configured — a notification
    # problem must never cost us the tick's work.
    if notify_batch and mode != "silent":
        try:
            from . import push

            best = max(notify_batch, key=lambda m: m[1])[0]
            push.notify_new_matches(
                user_id, len(notify_batch),
                f"{best.title or 'Role'} @ {best.company or best.source}")
        except Exception:  # noqa: BLE001
            logger.exception("push notify failed for %s", user_id)

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


# ---------------------------------------------------------------------------
# On-demand kick (quiz complete / pull-to-refresh)
# ---------------------------------------------------------------------------
# The scheduler still ticks every JOB_POLL_SECONDS. New testers cannot wait
# that long for a first match, and pull-to-refresh used to re-read an empty
# queue. These helpers start *one* pass per user, immediately, without
# blocking the HTTP response in production.

_kick_lock = threading.Lock()
_running: dict[str, float] = {}
_finished_at: dict[str, str] = {}
_finished_ts: dict[str, float] = {}
_MAX_CONCURRENT = 2
_COOLDOWN_SEC = 45.0


def reset_for_tests() -> None:
    """Drop in-flight kick state between tests."""
    with _kick_lock:
        _running.clear()
        _finished_at.clear()
        _finished_ts.clear()


def search_status(user_id: str) -> dict:
    """What the Apply tab should show: searching now vs last finished pass."""
    uid = (user_id or "").strip()
    with _kick_lock:
        started = _running.get(uid)
        return {
            "searching": uid in _running,
            "started_at": (
                datetime.fromtimestamp(started, tz=timezone.utc).isoformat()
                if started else None
            ),
            "last_finished_at": _finished_at.get(uid),
        }


def kick(user_id: str, *, force: bool = False) -> dict:
    """Start a discovery pass for this user if one isn't already running.

    Returns immediately. ``started`` is True when a new pass was launched.
    Under pytest the tick runs inline so the temp DB isn't unlinked under a
    background thread.
    """
    uid = (user_id or "").strip()
    if not uid:
        return {"started": False, "reason": "no_user", **search_status("")}
    if not profile.has_profile(uid):
        return {"started": False, "reason": "no_profile", **search_status(uid)}

    with _kick_lock:
        if uid in _running:
            return {"started": False, "reason": "already_running", **_status_unlocked(uid)}
        last = _finished_ts.get(uid)
        if not force and last is not None and (time.time() - last) < _COOLDOWN_SEC:
            return {"started": False, "reason": "cooldown", **_status_unlocked(uid)}
        if len(_running) >= _MAX_CONCURRENT:
            return {"started": False, "reason": "busy", **_status_unlocked(uid)}
        _running[uid] = time.time()

    if os.environ.get("PYTEST_CURRENT_TEST"):
        _run_kicked_tick(uid)
    else:
        threading.Thread(
            target=_run_kicked_tick, args=(uid,), daemon=True,
            name=f"discover-{uid[:16]}",
        ).start()
    return {"started": True, "reason": "ok", **search_status(uid)}


def _status_unlocked(user_id: str) -> dict:
    started = _running.get(user_id)
    return {
        "searching": user_id in _running,
        "started_at": (
            datetime.fromtimestamp(started, tz=timezone.utc).isoformat()
            if started else None
        ),
        "last_finished_at": _finished_at.get(user_id),
    }


def _run_kicked_tick(user_id: str) -> None:
    global last_tick_at
    try:
        tick(user_id)
    except Exception:  # noqa: BLE001 — HTTP already returned; log and clear
        logger.exception("on-demand discovery failed for %s", user_id)
    finally:
        now = datetime.now(timezone.utc)
        with _kick_lock:
            _running.pop(user_id, None)
            _finished_at[user_id] = now.isoformat()
            _finished_ts[user_id] = time.time()
            last_tick_at = now.isoformat()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from .db import init_db  # self-sufficient when run as a one-shot (e.g. cron)

    init_db()
    sent = run_all()
    print(f"Discovery sent {sent} new alert(s).")
