"""Greenhouse public job board API (free, no auth).

    GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true

``token`` is the board name in the careers URL, e.g. boards.greenhouse.io/<token>.
"""
from __future__ import annotations

from .base import JobPosting, get_json, strip_html

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def _parse(data, board_token: str) -> list[JobPosting]:
    if not isinstance(data, dict):
        return []
    out: list[JobPosting] = []
    for j in data.get("jobs") or []:
        ext = j.get("id")
        if ext is None:
            continue
        loc = (j.get("location") or {}).get("name") or ""
        out.append(
            JobPosting(
                source="greenhouse",
                external_id=str(ext),
                title=(j.get("title") or "").strip(),
                url=j.get("absolute_url") or "",
                company=(j.get("company_name") or board_token).strip(),
                location=loc.strip(),
                description=strip_html(j.get("content")),
                posted_at=(j.get("updated_at") or j.get("first_published") or "") or "",
            )
        )
    return out


def fetch(board_token: str) -> list[JobPosting]:
    return _parse(get_json(API.format(token=board_token)), board_token)
