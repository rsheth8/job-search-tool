"""Paid job aggregator — a SerpApi-style Google Jobs search (Phase 3).

Unlike the free ATS adapters (greenhouse/lever/ashby), this one searches the
*whole web* for roles matching the user's profile, not a single company board.
It costs money per call, so it is OFF by default and gated three ways, mirroring
the Apollo credit guards in ``app/apollo.py``:

  * ``AGGREGATOR_SEARCH_ENABLED=true`` AND an ``AGGREGATOR_API_KEY``
    (the ``aggregator_active`` property) — either alone does nothing.
  * a DB-backed daily call budget (``AGGREGATOR_MAX_CALLS_PER_DAY``, UTC day) so
    the cap survives restarts.
  * a per-minute token-bucket rate limit.

Contract, same as every adapter and the Apollo layer: **never raise, never
block.** ``fetch`` returns ``[]`` on no-key, disabled, over-budget, network
error, or bad payload — discovery just gets nothing that tick.

``board_token`` here is the *search query* (e.g. "new grad software engineer
remote"), not a company slug. Because it is search-based (and paid) it lives in
``jobsources.NON_BOARD_SOURCES`` so ``resolve_board`` never slug-probes it.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..config import get_settings
from ..db import connect
from ..ratelimit import TokenBucket
from .base import JobPosting, strip_html

logger = logging.getLogger("jobsources")

# SerpApi Google Jobs engine. Any SerpApi-compatible host returning the same
# ``jobs_results`` shape works; swap the URL via a fork if you self-host.
_API_URL = "https://serpapi.com/search"
_TIMEOUT_SECONDS = 12.0

_limiter: TokenBucket | None = None
_usage = {
    "searches": 0,
    "skipped_rate_limit": 0,
    "skipped_daily_cap": 0,
    "errors": 0,
}


# ---------------------------------------------------------------------------
# Budget tracking (DB-backed daily cap + in-memory counters)
# ---------------------------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _calls_today() -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM aggregator_api_calls WHERE called_at >= ?",
            (_today_start_iso(),),
        ).fetchone()
        return int(row["n"]) if row else 0


def _record_call(query: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO aggregator_api_calls (query, called_at) VALUES (?, ?)",
            (query, _utc_now_iso()),
        )
    _usage["searches"] += 1


def _get_limiter() -> TokenBucket:
    global _limiter
    if _limiter is None:
        _limiter = TokenBucket(get_settings().aggregator_rate_limit_per_min)
    return _limiter


def reset_for_tests() -> None:
    """Clear the cached limiter + in-memory counters (tests only)."""
    global _limiter
    _limiter = None
    for k in _usage:
        _usage[k] = 0


def usage() -> dict:
    """Counters for /health — today's search count vs the daily cap."""
    s = get_settings()
    return {
        **_usage,
        "active": s.aggregator_active,
        "calls_today": _calls_today(),
        "daily_cap": s.aggregator_max_calls_per_day,
    }


# ---------------------------------------------------------------------------
# Parsing (pure — fixture-tested)
# ---------------------------------------------------------------------------

def _parse(data, query: str = "") -> list[JobPosting]:
    """Map a SerpApi Google-Jobs response to JobPostings. Garbage -> []."""
    if not isinstance(data, dict):
        return []
    out: list[JobPosting] = []
    for j in data.get("jobs_results") or []:
        if not isinstance(j, dict):
            continue
        title = (j.get("title") or "").strip()
        job_id = j.get("job_id")
        # Prefer a direct apply link; fall back to the shareable listing URL.
        url = ""
        opts = j.get("apply_options") or []
        if opts and isinstance(opts[0], dict):
            url = opts[0].get("link") or ""
        url = url or j.get("share_link") or ""
        ext = str(job_id) if job_id else (url or title)
        if not ext or not (title or url):
            continue
        ext_info = j.get("detected_extensions") or {}
        out.append(
            JobPosting(
                source="aggregator",
                external_id=ext,
                title=title,
                url=url,
                company=(j.get("company_name") or "").strip(),
                location=(j.get("location") or "").strip(),
                description=strip_html(j.get("description")),
                posted_at=(ext_info.get("posted_at") or "").strip(),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Fetch (gated + budget-capped; never raises)
# ---------------------------------------------------------------------------

def fetch(board_token: str) -> list[JobPosting]:
    """Run one aggregator search for the query ``board_token``.

    Returns ``[]`` on every guard rail — disabled, no key, over daily budget,
    rate-limited, network/parse error — so a discovery tick never blocks on it.
    """
    query = (board_token or "").strip()
    if not query:
        return []
    s = get_settings()
    if not s.aggregator_active:
        return []
    if _calls_today() >= s.aggregator_max_calls_per_day:
        _usage["skipped_daily_cap"] += 1
        logger.info(
            "aggregator daily cap reached (%s/day); skipping %r",
            s.aggregator_max_calls_per_day, query,
        )
        return []
    if not _get_limiter().allow():
        _usage["skipped_rate_limit"] += 1
        logger.info("aggregator rate limited; skipping %r", query)
        return []

    import httpx  # lazy: offline/test paths never import it

    params = {"engine": "google_jobs", "q": query, "api_key": s.aggregator_api_key}
    if s.aggregator_location.strip():
        params["location"] = s.aggregator_location.strip()
    try:
        resp = httpx.get(_API_URL, params=params, timeout=_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001 — degrade to [] on any error
        _usage["errors"] += 1
        logger.warning("aggregator fetch failed for %r", query, exc_info=True)
        return []

    _record_call(query)  # count only billable calls that actually completed
    return _parse(data, query)[: s.aggregator_results_per_call]
