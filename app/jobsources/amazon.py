"""Amazon.jobs public search JSON (no auth).

    GET https://www.amazon.jobs/en/search.json?base_query=…&result_limit=25

Same class of thing as Greenhouse's public board: the careers site's own
JSON. ``board_token`` is the search query (a role from the user's profile).
Always cap results — amazon.jobs is huge and mostly warehouse without a query.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from ..config import get_settings
from .base import JobPosting, get_json, strip_html

logger = logging.getLogger("jobsources.amazon")

API = "https://www.amazon.jobs/en/search.json"
SITE = "https://www.amazon.jobs"


def _url(job: dict) -> str:
    path = (job.get("job_path") or job.get("url_next_step") or "").strip()
    if path.startswith("http"):
        return path
    if path.startswith("/"):
        return SITE + path
    job_id = str(job.get("id_icims") or job.get("id") or "").strip()
    if job_id:
        return f"{SITE}/en/jobs/{job_id}"
    return ""


def _parse(data, query: str) -> list[JobPosting]:
    if not isinstance(data, dict):
        return []
    out: list[JobPosting] = []
    for j in data.get("jobs") or []:
        if not isinstance(j, dict):
            continue
        ext = str(j.get("id_icims") or j.get("id") or "").strip()
        if not ext:
            continue
        loc = (j.get("location") or j.get("city") or "").strip()
        desc = j.get("description_short") or j.get("description") or ""
        out.append(
            JobPosting(
                source="amazon",
                external_id=ext,
                title=(j.get("title") or "").strip(),
                url=_url(j),
                company=(j.get("company_name") or "Amazon").strip() or "Amazon",
                location=loc,
                description=strip_html(desc),
                posted_at=str(j.get("posted_date") or j.get("posted") or "").strip(),
            )
        )
    return out


def fetch(board_token: str) -> list[JobPosting]:
    query = (board_token or "").strip() or "software engineer"
    if query.lower() in ("jobs", "amazon", "default"):
        query = "software engineer"
    cap = max(1, int(get_settings().job_amazon_max_jobs or 25))
    data = get_json(
        API,
        params={
            "base_query": query,
            "offset": 0,
            "result_limit": min(100, cap),
            "sort": "recent",
        },
    )
    return _parse(data, query)[:cap]


_JOB_PATH = re.compile(
    r"/jobs/(\d+)(?:/([^/?#]+))?",
    re.I,
)


def parse_job_url(url: str) -> JobPosting | None:
    """Amazon.jobs posting URL → JobPosting, no network."""
    try:
        parsed = urlparse((url or "").strip())
    except Exception:  # noqa: BLE001
        return None
    host = (parsed.hostname or "").lower()
    if not host.endswith("amazon.jobs"):
        return None
    m = _JOB_PATH.search(parsed.path or "")
    if not m:
        return None
    ext, slug = m.group(1), m.group(2) or ""
    title = re.sub(r"[-_]+", " ", slug).strip().title() or "Amazon job"
    return JobPosting(
        source="amazon",
        external_id=ext,
        title=title,
        url=f"{SITE}/en/jobs/{ext}" + (f"/{slug}" if slug else ""),
        company="Amazon",
        description=title,
    )
