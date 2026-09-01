"""USAJobs Search API — official, free, key required.

Register at developer.usajobs.gov. The key lives on the server
(``USAJOBS_API_KEY``); users never see it. No key → ``[]`` (discovery
continues on every other source).

``board_token`` is the keyword query (a role from the profile).
"""
from __future__ import annotations

import logging

from ..config import get_settings
from .base import JobPosting, get_json, strip_html

logger = logging.getLogger("jobsources.usajobs")

API = "https://data.usajobs.gov/api/search"


def _parse(data) -> list[JobPosting]:
    if not isinstance(data, dict):
        return []
    items = ((data.get("SearchResult") or {}).get("SearchResultItems") or [])
    out: list[JobPosting] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        desc = row.get("MatchedObjectDescriptor") or {}
        if not isinstance(desc, dict):
            continue
        ext = str(
            desc.get("PositionID") or row.get("MatchedObjectId") or ""
        ).strip()
        if not ext:
            continue
        details = ((desc.get("UserArea") or {}).get("Details") or {})
        summary = details.get("JobSummary") if isinstance(details, dict) else ""
        loc = (desc.get("PositionLocationDisplay") or "").strip()
        out.append(
            JobPosting(
                source="usajobs",
                external_id=ext,
                title=(desc.get("PositionTitle") or "").strip(),
                url=(desc.get("PositionURI") or "").strip(),
                company=(desc.get("OrganizationName") or "USAJobs").strip(),
                location=loc,
                description=strip_html(summary or desc.get("QualificationSummary")),
                posted_at=(desc.get("PublicationStartDate") or "").strip(),
            )
        )
    return out


def fetch(board_token: str) -> list[JobPosting]:
    s = get_settings()
    key = (s.usajobs_api_key or "").strip()
    email = (s.usajobs_user_agent or "").strip()
    if not key or not email:
        return []
    query = (board_token or "").strip() or "software engineer"
    if query.lower() in ("jobs", "usajobs", "default"):
        query = "software engineer"
    cap = max(1, int(s.job_usajobs_max_jobs or 25))
    data = get_json(
        API,
        params={
            "Keyword": query,
            "ResultsPerPage": min(50, cap),
            "SortField": "opendate",
            "SortDirection": "desc",
            "DatePosted": 30,
        },
        # The only adapter that does not send User-Agent: JobPilot/1.0, and it
        # is not spoofing: developer.usajobs.gov requires the User-Agent to be
        # the email address the API key was registered to, and rejects the
        # request otherwise. Do not "restore" the standard UA here.
        extra_headers={
            "Host": "data.usajobs.gov",
            "User-Agent": email,
            "Authorization-Key": key,
        },
    )
    return _parse(data)[:cap]
