"""Wide job discovery: RSS, ATS directory, Simplify, YC, Workday, Amazon, Netflix, USAJobs.

Runs for users with a job-search profile (even with zero tracked companies).
Postings merge into the same ``discovery.tick`` pipeline as board tracking.
"""
from __future__ import annotations

import logging
import sqlite3

from .config import get_settings
from . import catalog, eligibility
from .jobsources import JobPosting, fetch_source
from .jobsources import directory as dir_src
from .jobsources import rss as rss_src
from .jobsources import swelist as swelist_src

logger = logging.getLogger("wide_discovery")

# Wide sources skip heavy seeding (would baseline thousands of roles).
_NO_SEED_SOURCES = frozenset({
    "rss", "directory", "swelist", "yc", "amazon", "netflix", "usajobs",
})


def is_wide_source(source: str) -> bool:
    return (source or "").lower() in _NO_SEED_SOURCES


def _profile_search_text(prof: sqlite3.Row | None) -> str:
    if prof is None:
        return ""
    parts: list[str] = []
    for field in ("roles", "keywords", "seniority", "resume_summary"):
        try:
            parts.append(prof[field] or "")
        except (IndexError, KeyError):
            pass
    return " ".join(parts)


def _search_query(prof: sqlite3.Row | None) -> str:
    """Role phrase to send to Amazon / Netflix / USAJobs search. First profile role."""
    if prof is None:
        return "software engineer"
    try:
        raw = (prof["roles"] or "").strip()
    except (IndexError, KeyError, TypeError):
        raw = ""
    clause = next(
        (c.strip() for c in raw.replace(";", ",").split(",") if c.strip()),
        "",
    )
    return (clause or "software engineer")[:80]


def wide_rss_feed_ids(prof: sqlite3.Row | None = None) -> list[str]:
    """Env-configured feed ids, plus We Work Remotely categories the profile matches."""
    s = get_settings()
    if not s.job_wide_rss_enabled or "rss" not in s.job_sources:
        return []
    technical = eligibility.profile_looks_technical(prof)
    out: list[str] = []
    for raw in (s.job_wide_rss_feeds or "").split(","):
        fid = raw.strip().lower()
        if fid and fid not in out and fid in rss_src.FEEDS:
            # Programming WWR is the env default for CS users; skip it when the
            # profile is clearly non-technical so sales/design/etc. aren't
            # flooded with SWE listings.
            if fid == "weworkremotely" and not technical:
                continue
            out.append(fid)
    for fid in rss_src.feeds_for_profile_text(_profile_search_text(prof)):
        if fid not in out:
            out.append(fid)
    return out


def collect_fresh(
    user_id: str,
    prof: sqlite3.Row | None,
    *,
    existing_keys: set[tuple[str, str]] | None = None,
) -> list[JobPosting]:
    """Pull new postings from RSS, ATS directory, swelist, YC, Workday, Amazon, Netflix, USAJobs."""
    if prof is None:
        return []
    settings = get_settings()
    sources = set(settings.job_sources)
    seen = existing_keys or set()
    incoming: list[JobPosting] = []
    sectors = catalog.directory_sectors(prof)

    if "rss" in sources and settings.job_wide_rss_enabled:
        for feed_id in wide_rss_feed_ids(prof):
            incoming.extend(fetch_source("rss", feed_id))

    if "directory" in sources and settings.job_wide_directory_enabled:
        incoming.extend(
            dir_src.fetch_directory_batch(user_id=user_id, sectors=sectors)
        )

    if "swelist" in sources and settings.job_wide_swelist_enabled:
        for list_id in swelist_src.configured_list_ids():
            incoming.extend(fetch_source("swelist", list_id))

    if "yc" in sources and settings.job_wide_yc_enabled:
        incoming.extend(fetch_source("yc", "jobs"))

    if "workday" in sources and settings.job_wide_workday_enabled:
        from .jobsources import workday as workday_src

        incoming.extend(
            workday_src.fetch_directory_batch(user_id=user_id, sectors=sectors)
        )

    query = _search_query(prof)
    if "amazon" in sources and settings.job_wide_amazon_enabled:
        incoming.extend(fetch_source("amazon", query))
    if "netflix" in sources and settings.job_wide_netflix_enabled:
        incoming.extend(fetch_source("netflix", query))
    if "usajobs" in sources and settings.job_wide_usajobs_enabled:
        incoming.extend(fetch_source("usajobs", query))

    dir_src.learn_from_postings(incoming)

    fresh: list[JobPosting] = []
    for p in incoming:
        key = (p.source, p.external_id)
        if p.external_id and key not in seen:
            seen.add(key)
            fresh.append(p)

    if fresh:
        logger.info("wide_discovery: %d fresh posting(s) for %s", len(fresh), user_id)
    return fresh


def ensure_default_feeds_tracked(user_id: str, prof: sqlite3.Row | None = None) -> int:
    """Auto-track default RSS feeds so users can list/untrack them. Returns added."""
    added = 0
    for feed_id in wide_rss_feed_ids(prof):
        meta = rss_src.resolve_feed(feed_id)
        if not meta:
            continue
        from . import jobstore

        row = jobstore.add_tracked_company(
            user_id, "rss", feed_id, meta["label"]
        )
        if row is not None:
            added += 1
    if (
        get_settings().job_wide_swelist_enabled
        and "swelist" in get_settings().job_sources
    ):
        from . import jobstore

        for list_id in swelist_src.configured_list_ids():
            meta = swelist_src.resolve_list(list_id)
            if not meta:
                continue
            row = jobstore.add_tracked_company(
                user_id, "swelist", meta["list_id"], meta["label"]
            )
            if row is not None:
                added += 1
    if get_settings().job_wide_yc_enabled and "yc" in get_settings().job_sources:
        from . import jobstore

        row = jobstore.add_tracked_company(
            user_id, "yc", "jobs", "Y Combinator jobs"
        )
        if row is not None:
            added += 1
    return added


def describe_wide_status(prof: sqlite3.Row | None = None) -> str:
    """One-line summary for profile confirmation."""
    s = get_settings()
    parts: list[str] = []
    if s.job_wide_rss_enabled and "rss" in s.job_sources:
        feeds = ", ".join(wide_rss_feed_ids(prof)) or "none"
        parts.append(f"RSS ({feeds})")
    if s.job_wide_directory_enabled and "directory" in s.job_sources:
        sectors = catalog.directory_sectors(prof)
        n = dir_src.board_count(sectors)
        refs = catalog.name_count()
        label = ", ".join(sorted(sectors))
        extra = f", {refs:,} company refs" if refs else ""
        parts.append(f"ATS directory ({label}: {n} live boards{extra}, rotating)")
    if s.job_wide_swelist_enabled and "swelist" in s.job_sources:
        lists = ", ".join(swelist_src.configured_list_ids())
        parts.append(f"Pitt CSC / Simplify lists ({lists})")
    if s.job_wide_yc_enabled and "yc" in s.job_sources:
        parts.append("Y Combinator jobs")
    if s.job_wide_workday_enabled and "workday" in s.job_sources:
        from .jobsources import workday as workday_src

        n = workday_src.board_count(catalog.directory_sectors(prof) if prof else None)
        parts.append(f"Workday careers ({n} companies, rotating)")
    if s.job_wide_amazon_enabled and "amazon" in s.job_sources:
        parts.append("Amazon.jobs")
    if s.job_wide_netflix_enabled and "netflix" in s.job_sources:
        parts.append("Netflix careers")
    if s.job_wide_usajobs_enabled and "usajobs" in s.job_sources:
        if s.usajobs_api_key.strip() and s.usajobs_user_agent.strip():
            parts.append("USAJobs")
    return "; ".join(parts) if parts else ""
