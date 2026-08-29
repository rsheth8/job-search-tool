"""Y Combinator public jobs landing page.

Parses the server-rendered Inertia ``data-page`` JSON on ycombinator.com/jobs
(featured postings). Apply CTAs often go through a YC login wall; we store the
public company job page (``/companies/<slug>/jobs/...``) so the link is
actionable without inventing an ATS URL.

``board_token`` is unused (wide source). Failures return ``[]``.
"""
from __future__ import annotations

import json
import logging
import re
from html import unescape

from .base import JobPosting, get_text, strip_html

logger = logging.getLogger("jobsources.yc")

LANDING = "https://www.ycombinator.com/jobs"
# Public role landings reuse the same data-page shape; extra pages are merged.
ROLE_PAGES = (
    LANDING,
    "https://www.ycombinator.com/jobs/software-engineer",
)
_DATA_PAGE_RE = re.compile(r'data-page="([^"]+)"')


def _job_url(raw: str) -> str:
    url = (raw or "").strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return "https://www.ycombinator.com" + url
    return ""


def _description(j: dict) -> str:
    bits: list[str] = []
    one = (j.get("companyOneLiner") or "").strip()
    if one:
        bits.append(one)
    batch = (j.get("companyBatchName") or j.get("companyBatch") or "").strip()
    if batch:
        bits.append(f"YC {batch}.")
    for key, label in (
        ("type", None),
        ("jobType", None),
        ("prettyRole", None),
        ("roleSpecificType", None),
        ("salaryRange", "Salary"),
        ("salary", "Salary"),
        ("equityRange", "Equity"),
        ("minExperience", "Experience"),
        ("visa", "Visa"),
    ):
        val = j.get(key)
        if val is None or val == "":
            continue
        text = str(val).strip()
        if not text:
            continue
        bits.append(f"{label}: {text}" if label else text)
    return strip_html(" ".join(bits))


def _posts_from_payload(data) -> list[JobPosting]:
    if not isinstance(data, dict):
        return []
    props = data.get("props") if isinstance(data.get("props"), dict) else data
    rows = props.get("jobPostings") or props.get("jobs") or []
    if not isinstance(rows, list):
        return []
    out: list[JobPosting] = []
    for j in rows:
        if not isinstance(j, dict):
            continue
        ext = j.get("id")
        title = (j.get("title") or "").strip()
        url = _job_url(j.get("url") or "")
        company = (j.get("companyName") or "").strip()
        if ext is None or not title or not url or not company:
            continue
        out.append(
            JobPosting(
                source="yc",
                external_id=str(ext),
                title=title,
                url=url,
                company=company,
                location=(j.get("location") or "").strip(),
                description=_description(j),
                posted_at=(j.get("createdAt") or "") or "",
            )
        )
    return out


def parse_inertia_html(html: str) -> list[JobPosting]:
    """Pure parse of a YC/WaaS HTML page. Never hits the network."""
    if not html:
        return []
    m = _DATA_PAGE_RE.search(html)
    if not m:
        return []
    try:
        data = json.loads(unescape(m.group(1)))
    except json.JSONDecodeError:
        return []
    return _posts_from_payload(data)


def fetch(board_token: str = "jobs") -> list[JobPosting]:
    seen: set[str] = set()
    out: list[JobPosting] = []
    for url in ROLE_PAGES:
        html = get_text(url, accept="text/html,application/xhtml+xml;q=0.9,*/*;q=0.8")
        if not html:
            continue
        for p in parse_inertia_html(html):
            if p.external_id in seen:
                continue
            seen.add(p.external_id)
            out.append(p)
    logger.info("yc: %d posting(s) from public jobs pages", len(out))
    return out
