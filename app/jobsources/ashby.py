"""Ashby public job board API (free, no auth).

    GET https://api.ashbyhq.com/posting-api/job-board/{token}

``token`` is the job-board name in jobs.ashbyhq.com/<token>. Response is
``{"jobs": [...]}``. Field names vary a little across boards, so access is
defensive with sensible fallbacks.
"""
from __future__ import annotations

from .base import JobPosting, get_json, strip_html

API = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=false"


def _parse(data, board_token: str) -> list[JobPosting]:
    if not isinstance(data, dict):
        return []
    out: list[JobPosting] = []
    for j in data.get("jobs") or []:
        ext = j.get("id")
        if not ext:
            continue
        location = j.get("location") or ""
        if j.get("isRemote") and "remote" not in location.lower():
            location = (location + " (remote)").strip() if location else "Remote"
        desc = j.get("descriptionPlain") or strip_html(j.get("descriptionHtml"))
        out.append(
            JobPosting(
                source="ashby",
                external_id=str(ext),
                title=(j.get("title") or "").strip(),
                url=j.get("jobUrl") or j.get("applyUrl") or "",
                company=(j.get("organizationName") or board_token).strip(),
                location=location.strip(),
                description=strip_html(desc) if desc else "",
                posted_at=(j.get("publishedAt") or j.get("updatedAt") or "") or "",
            )
        )
    return out


def fetch(board_token: str) -> list[JobPosting]:
    return _parse(get_json(API.format(token=board_token)), board_token)
