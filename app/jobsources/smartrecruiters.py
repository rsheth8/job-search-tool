"""SmartRecruiters public postings API (free, no auth).

    GET https://api.smartrecruiters.com/v1/companies/{token}/postings

``token`` is the company identifier in jobs.smartrecruiters.com/<token>/<id>.
Identifiers are often PascalCase (ServiceNow, WesternDigital) — do not
lowercase them when probing. Response is ``{"content": [...], "totalFound": N}``.
"""
from __future__ import annotations

from .base import JobPosting, get_json, strip_html

API = "https://api.smartrecruiters.com/v1/companies/{token}/postings?limit={limit}"
APPLY = "https://jobs.smartrecruiters.com/{token}/{posting_id}"
_PAGE = 100


def _location(j: dict) -> str:
    loc = j.get("location") or {}
    if not isinstance(loc, dict):
        return str(loc or "").strip()
    text = (loc.get("fullLocation") or "").strip()
    if not text:
        parts = [loc.get("city"), loc.get("region"), loc.get("country")]
        text = ", ".join(str(p).strip() for p in parts if p and str(p).strip())
    remote = loc.get("remote") or loc.get("hybrid")
    if remote and "remote" not in text.lower():
        kind = "Remote" if loc.get("remote") else "Hybrid"
        text = f"{text} ({kind})".strip() if text else kind
    return text


def _description(j: dict) -> str:
    bits: list[str] = []
    dept = ((j.get("department") or {}) if isinstance(j.get("department"), dict) else {})
    label = (dept.get("label") or "").strip()
    if label:
        bits.append(label)
    fn = j.get("function") or {}
    if isinstance(fn, dict) and fn.get("label"):
        bits.append(str(fn["label"]))
    emp = j.get("typeOfEmployment") or {}
    if isinstance(emp, dict) and emp.get("label"):
        bits.append(str(emp["label"]))
    exp = j.get("experienceLevel") or {}
    if isinstance(exp, dict) and exp.get("label"):
        bits.append(str(exp["label"]))
    return strip_html(". ".join(bits))


def _parse(data, board_token: str) -> list[JobPosting]:
    if not isinstance(data, dict):
        return []
    rows = data.get("content")
    if not isinstance(rows, list):
        return []
    out: list[JobPosting] = []
    for j in rows:
        if not isinstance(j, dict):
            continue
        ext = j.get("id") or j.get("uuid")
        title = (j.get("name") or "").strip()
        if not ext or not title:
            continue
        company = ""
        co = j.get("company") or {}
        if isinstance(co, dict):
            company = (co.get("name") or co.get("identifier") or "").strip()
        out.append(
            JobPosting(
                source="smartrecruiters",
                external_id=str(ext),
                title=title,
                url=APPLY.format(token=board_token, posting_id=ext),
                company=company or board_token,
                location=_location(j),
                description=_description(j),
                posted_at=(j.get("releasedDate") or "") or "",
            )
        )
    return out


def fetch(board_token: str) -> list[JobPosting]:
    token = (board_token or "").strip()
    if not token:
        return []
    data = get_json(API.format(token=token, limit=_PAGE))
    return _parse(data, token)
