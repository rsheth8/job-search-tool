"""Netflix careers — Eightfold's public apply JSON (no auth).

    GET https://explore.jobs.netflix.net/api/apply/v2/jobs?domain=netflix.com&query=…

Same class as Amazon.jobs ``search.json`` / Greenhouse boards: the careers
site's own JSON, identified as ``JobPilot/1.0``. 403/429 → skip, never spoof
a browser. ``board_token`` is the search query (a role from the profile).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from ..config import get_settings
from .base import JobPosting, get_json, strip_html

logger = logging.getLogger("jobsources.netflix")

API = "https://explore.jobs.netflix.net/api/apply/v2/jobs"
SITE = "https://explore.jobs.netflix.net"
DOMAIN = "netflix.com"


def _url(job: dict, ext: str) -> str:
    path = (job.get("canonicalPositionUrl") or job.get("positionUrl") or "").strip()
    if path.startswith("http"):
        return path.split("?")[0]
    if path.startswith("/"):
        return SITE + path.split("?")[0]
    return f"{SITE}/careers/job/{ext}"


def _location(job: dict) -> str:
    loc = job.get("location")
    if isinstance(loc, str) and loc.strip():
        return loc.strip()
    locs = job.get("locations")
    if isinstance(locs, list):
        parts = [str(x).strip() for x in locs if str(x).strip()]
        return "; ".join(parts)
    return ""


def _posted_at(job: dict) -> str:
    ts = job.get("t_create") or job.get("posted_ts") or job.get("t_update")
    try:
        n = int(ts)
    except (TypeError, ValueError):
        return str(ts or "").strip()
    if n > 10_000_000_000:  # milliseconds
        n //= 1000
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _parse(data) -> list[JobPosting]:
    if not isinstance(data, dict):
        return []
    out: list[JobPosting] = []
    for j in data.get("positions") or []:
        if not isinstance(j, dict):
            continue
        ext = str(j.get("id") or j.get("ats_job_id") or j.get("display_job_id") or "").strip()
        if not ext:
            continue
        title = (j.get("name") or j.get("posting_name") or "").strip()
        desc = j.get("job_description") or j.get("department") or title
        out.append(
            JobPosting(
                source="netflix",
                external_id=ext,
                title=title,
                url=_url(j, ext),
                company="Netflix",
                location=_location(j),
                description=strip_html(desc),
                posted_at=_posted_at(j),
            )
        )
    return out


def fetch(board_token: str) -> list[JobPosting]:
    query = (board_token or "").strip() or "software engineer"
    if query.lower() in ("jobs", "netflix", "default"):
        query = "software engineer"
    cap = max(1, int(get_settings().job_netflix_max_jobs or 25))
    data = get_json(
        API,
        params={
            "domain": DOMAIN,
            "start": 0,
            "num": min(50, cap),
            "query": query,
        },
        # No Referer — see the note in workday.py. We identify as JobPilot and
        # ask; we do not imply a page visit that never happened.
    )
    return _parse(data)[:cap]


_JOB_PATH = re.compile(r"/careers/job/(\d+)", re.I)


def parse_job_url(url: str) -> JobPosting | None:
    """Netflix career posting URL → JobPosting, no network."""
    try:
        parsed = urlparse((url or "").strip())
    except Exception:  # noqa: BLE001
        return None
    host = (parsed.hostname or "").lower()
    if not (host.endswith("jobs.netflix.net") or host == "jobs.netflix.com"):
        return None
    m = _JOB_PATH.search(parsed.path or "")
    if not m:
        return None
    ext = m.group(1)
    slug = (parsed.path or "").rstrip("/").split("/")[-1]
    title = "Netflix job"
    if "-" in slug:
        title = re.sub(r"[-_]+", " ", slug.split("-", 1)[-1]).strip().title() or title
    return JobPosting(
        source="netflix",
        external_id=ext,
        title=title,
        url=f"{SITE}/careers/job/{ext}",
        company="Netflix",
        description=title,
    )


def lookup_company(name: str) -> dict | None:
    """``track openings at Netflix`` — name match only, never guessed."""
    if (name or "").strip().lower() != "netflix":
        return None
    return {"source": "netflix", "token": "jobs", "name": "Netflix"}
