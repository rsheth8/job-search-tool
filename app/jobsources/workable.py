"""Workable public widget API (free, no auth).

    GET https://apply.workable.com/api/v1/widget/accounts/{token}

``token`` is the company slug in apply.workable.com/<token>. Response is
``{"name": ..., "jobs": [...]}``. Empty or missing boards return ``[]``.
"""
from __future__ import annotations

from .base import JobPosting, get_json, strip_html

API = "https://apply.workable.com/api/v1/widget/accounts/{token}"


def _location(j: dict) -> str:
    loc = j.get("location")
    if isinstance(loc, str):
        text = loc.strip()
    elif isinstance(loc, dict):
        parts = [loc.get("city"), loc.get("region") or loc.get("state"), loc.get("country")]
        text = ", ".join(str(p).strip() for p in parts if p and str(p).strip())
        remote = loc.get("telecommuting") or loc.get("remote")
        if remote and "remote" not in text.lower():
            text = (text + " (remote)").strip() if text else "Remote"
    else:
        text = ""
    if j.get("remote") and "remote" not in text.lower():
        text = (text + " (remote)").strip() if text else "Remote"
    return text


def _url(j: dict, board_token: str) -> str:
    for key in ("url", "shortlink", "application_url"):
        raw = (j.get(key) or "").strip()
        if raw.startswith("http"):
            return raw
    code = (j.get("shortcode") or "").strip()
    if code:
        return f"https://apply.workable.com/{board_token}/j/{code}/"
    return ""


def _parse(data, board_token: str) -> list[JobPosting]:
    if not isinstance(data, dict):
        return []
    company = (data.get("name") or board_token).strip()
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        return []
    out: list[JobPosting] = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        ext = j.get("shortcode") or j.get("id") or j.get("code")
        title = (j.get("title") or "").strip()
        url = _url(j, board_token)
        if not ext or not title or not url:
            continue
        desc = j.get("description") or j.get("descriptionPlain") or ""
        dept = (j.get("department") or "").strip()
        if dept and dept.lower() not in (desc or "").lower():
            desc = f"{dept}. {desc}".strip() if desc else dept
        out.append(
            JobPosting(
                source="workable",
                external_id=str(ext),
                title=title,
                url=url,
                company=company,
                location=_location(j),
                description=strip_html(desc) if desc else "",
                posted_at=(j.get("published_on") or j.get("created_at") or "") or "",
            )
        )
    return out


def fetch(board_token: str) -> list[JobPosting]:
    token = (board_token or "").strip()
    if not token:
        return []
    return _parse(get_json(API.format(token=token)), token)
