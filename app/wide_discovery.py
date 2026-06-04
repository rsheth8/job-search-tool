"""Wide job discovery: RSS feeds, SerpApi search, ATS directory rotation.

Runs for users with a job-search profile (even with zero tracked companies).
Postings merge into the same ``discovery.tick`` pipeline as board tracking.
"""
from __future__ import annotations

import logging
import sqlite3

from .config import get_settings
from .jobsources import JobPosting, fetch_source
from .jobsources import aggregator as agg_src
from .jobsources import directory as dir_src
from .jobsources import rss as rss_src
logger = logging.getLogger("wide_discovery")

# Wide sources skip heavy seeding (would baseline thousands of roles).
_NO_SEED_SOURCES = frozenset({"rss", "aggregator", "directory"})


def is_wide_source(source: str) -> bool:
    return (source or "").lower() in _NO_SEED_SOURCES


def wide_rss_feed_ids() -> list[str]:
    s = get_settings()
    if not s.job_wide_rss_enabled or "rss" not in s.job_sources:
        return []
    out: list[str] = []
    for raw in (s.job_wide_rss_feeds or "").split(","):
        fid = raw.strip().lower()
        if fid and fid not in out and fid in rss_src.FEEDS:
            out.append(fid)
    return out


def collect_fresh(
    user_id: str,
    prof: sqlite3.Row | None,
    *,
    existing_keys: set[tuple[str, str]] | None = None,
) -> list[JobPosting]:
    """Pull postings from RSS, aggregator, and directory (profile required)."""
    if prof is None:
        return []
    settings = get_settings()
    sources = set(settings.job_sources)
    seen = existing_keys or set()
    fresh: list[JobPosting] = []

    if "rss" in sources and settings.job_wide_rss_enabled:
        for feed_id in wide_rss_feed_ids():
            for p in fetch_source("rss", feed_id):
                key = (p.source, p.external_id)
                if p.external_id and key not in seen:
                    seen.add(key)
                    fresh.append(p)

    if "aggregator" in sources and settings.job_wide_aggregator_enabled:
        if settings.serpapi_enabled:
            query = agg_src.build_query_from_profile(
                prof["roles"], prof["locations"], prof["seniority"]
            )
            for p in fetch_source("aggregator", query):
                key = (p.source, p.external_id)
                if p.external_id and key not in seen:
                    seen.add(key)
                    fresh.append(p)
        else:
            logger.debug("aggregator enabled but SERPAPI_API_KEY unset")

    if "directory" in sources and settings.job_wide_directory_enabled:
        for p in dir_src.fetch_directory_batch():
            key = (p.source, p.external_id)
            if p.external_id and key not in seen:
                seen.add(key)
                fresh.append(p)

    if fresh:
        logger.info("wide_discovery: %d fresh posting(s) for %s", len(fresh), user_id)
    return fresh


def ensure_default_feeds_tracked(user_id: str) -> int:
    """Auto-track default RSS feeds so users can list/untrack them. Returns added."""
    added = 0
    for feed_id in wide_rss_feed_ids():
        meta = rss_src.resolve_feed(feed_id)
        if not meta:
            continue
        from . import jobstore

        row = jobstore.add_tracked_company(
            user_id, "rss", feed_id, meta["label"]
        )
        if row is not None:
            added += 1
    return added


def describe_wide_status() -> str:
    """One-line summary for profile confirmation."""
    s = get_settings()
    parts: list[str] = []
    if s.job_wide_rss_enabled and "rss" in s.job_sources:
        feeds = ", ".join(wide_rss_feed_ids()) or "none"
        parts.append(f"RSS ({feeds})")
    if s.job_wide_directory_enabled and "directory" in s.job_sources:
        parts.append(f"ATS directory ({dir_src.board_count()} boards, rotating)")
    if s.job_wide_aggregator_enabled and "aggregator" in s.job_sources:
        parts.append("Google Jobs search" + (" ✓" if s.serpapi_enabled else " — needs SERPAPI_API_KEY"))
    return "; ".join(parts) if parts else ""
