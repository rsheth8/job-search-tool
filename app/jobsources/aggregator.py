"""Paid job search via SerpApi Google Jobs (optional).

``board_token`` is the search query string (built from the user's profile in
``wide_discovery``). Requires ``SERPAPI_API_KEY`` and ``aggregator`` in
``JOB_SOURCES_ENABLED``.
"""
from __future__ import annotations

import hashlib
import logging

from .base import JobPosting

logger = logging.getLogger("jobsources.aggregator")

_ENGINE = "google_jobs"


def build_query_from_profile(roles: str | None, locations: str | None, seniority: str | None) -> str:
    """Turn profile fields into a single Google Jobs query."""
    parts: list[str] = []
    if roles:
        parts.append(roles.split(",")[0].strip())
    elif seniority:
        parts.append(seniority.strip())
    else:
        parts.append("software engineer")
    if locations:
        loc = locations.split(",")[0].strip()
        if loc.lower() != "remote":
            parts.append(f"in {loc}")
        else:
            parts.append("remote")
    return " ".join(parts)[:200]


def fetch(board_token: str) -> list[JobPosting]:
    from ..config import get_settings

    s = get_settings()
    key = (getattr(s, "serpapi_api_key", None) or "").strip()
    if not key:
        return []

    from .. import jobstore

    if not jobstore.allow_aggregator_search():
        logger.info("aggregator daily cap reached — skipping")
        return []

    q = (board_token or "software engineer").strip()
    url = "https://serpapi.com/search.json"
    params = {
        "engine": _ENGINE,
        "q": q,
        "api_key": key,
        "hl": "en",
        "gl": "us",
    }
    import httpx

    try:
        resp = httpx.get(url, params=params, timeout=15.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001
        logger.warning("aggregator fetch failed", exc_info=True)
        return []

    jobstore.record_aggregator_call()
    return _parse(data, q)


def _parse(data: dict, query: str) -> list[JobPosting]:
    if not isinstance(data, dict):
        return []
    out: list[JobPosting] = []
    for j in data.get("jobs_results") or []:
        if not isinstance(j, dict):
            continue
        title = (j.get("title") or "").strip()
        company = (j.get("company_name") or "").strip()
        if not title:
            continue
        loc = (j.get("location") or "").strip()
        link = (j.get("share_link") or j.get("apply_link") or "").strip()
        desc = (j.get("description") or "").strip()
        if len(desc) > 1500:
            desc = desc[:1500] + "…"
        jid = j.get("job_id") or link or f"{company}:{title}"
        ext = hashlib.sha256(f"{query}:{jid}".encode()).hexdigest()[:32]
        out.append(
            JobPosting(
                source="aggregator",
                external_id=ext,
                title=title,
                url=link,
                company=company or "Unknown",
                location=loc,
                description=desc,
                posted_at=(j.get("detected_extensions") or {}).get("posted_at", "")
                if isinstance(j.get("detected_extensions"), dict)
                else "",
            )
        )
    return out
