"""Turn a pasted job URL into a JobPosting — no crawl of LinkedIn/Indeed.

The user found the job (LinkedIn, Indeed, a Workday req, Amazon, a Greenhouse
form). We parse what the URL already contains and save it. We do not fetch
linkedin.com or indeed.com.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, urlparse

from .. import ats
from . import amazon, netflix, workday
from .base import JobPosting

_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
_FILLER = re.compile(
    r"^\s*(save|add|track|open|check(?:\s+out)?|look\s+at|this(\s+one)?|"
    r"job\s+link|link|please|can you|please save)?\s*",
    re.I,
)


def extract_url(text: str) -> str | None:
    m = _URL_RE.search(text or "")
    if not m:
        return None
    return m.group(0).rstrip(").,]>\"'")


def is_job_link_message(text: str) -> bool:
    """True when the message is basically a URL (plus a few words)."""
    url = extract_url(text)
    if not url:
        return False
    rest = _URL_RE.sub("", text or "")
    rest = _FILLER.sub("", rest).strip()
    if re.search(r"\b(note|applied|remind|deadline|delete|status)\b", rest, re.I):
        return False
    return len(rest) <= 48


def _slug_title(slug: str) -> str:
    return re.sub(r"[-_]+", " ", (slug or "").strip()).strip().title()


def _link_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def from_url(raw: str) -> JobPosting | None:
    """Best-effort parse. Never hits LinkedIn/Indeed. None if there's no URL."""
    url = extract_url(raw) or (raw or "").strip()
    if not url.startswith("http"):
        return None

    wd = workday.parse_job_url(url)
    if wd is not None:
        return wd
    amz = amazon.parse_job_url(url)
    if amz is not None:
        return amz
    nflx = netflix.parse_job_url(url)
    if nflx is not None:
        return nflx

    ref = ats.posting_ref(url)
    if ref is not None:
        source, token, job_id = ref
        company = token.replace("-", " ").title()
        return JobPosting(
            source=source,
            external_id=str(job_id),
            title="",
            url=url,
            company=company,
            description="",
        )

    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return None
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    qs = parse_qs(parsed.query)

    if "linkedin.com" in host:
        m = re.search(r"/jobs/view/(\d+)", path)
        ext = m.group(1) if m else _link_id(url)
        return JobPosting(
            source="link",
            external_id=f"linkedin:{ext}",
            title="LinkedIn job",
            url=url.split("?")[0],
            company="",
            description="Pasted from LinkedIn. Open in the in-app browser to apply.",
        )
    if "indeed.com" in host:
        jk = (qs.get("jk") or [""])[0] or _link_id(url)
        return JobPosting(
            source="link",
            external_id=f"indeed:{jk}",
            title="Indeed job",
            url=url,
            company="",
            description="Pasted from Indeed. Open in the in-app browser to apply.",
        )
    if host.endswith("usajobs.gov"):
        m = re.search(r"/job/(\d+)", path)
        ext = m.group(1) if m else _link_id(url)
        return JobPosting(
            source="usajobs",
            external_id=ext,
            title=_slug_title(path.rstrip("/").split("/")[-1]) or "USAJobs listing",
            url=url.split("?")[0],
            company="USAJobs",
        )

    title = _slug_title(path.rstrip("/").split("/")[-1])
    if title.lower() in {"jobs", "job", "apply", "view", "posting", ""}:
        title = "Saved job"
    return JobPosting(
        source="link",
        external_id=_link_id(url),
        title=title,
        url=url,
        company="",
        description="Pasted job link.",
    )


def save_pasted_job(user_id: str, raw: str) -> dict:
    """Parse a pasted URL, queue it, and stage it for Apply. Never crawls LinkedIn."""
    from .. import apply_queue, ats, jobstore

    posting = from_url(raw)
    if posting is None:
        return {"ok": False, "error": "Need a full https:// job URL."}
    if not (posting.title or "").strip():
        posting.title = "Saved job"
    row = jobstore.save_posting(
        user_id, posting, relevance_score=0.8, status="queued",
    )
    created = row is not None
    if row is None:
        row = jobstore.get_by_external(user_id, posting.source, posting.external_id)
    if row is None:
        return {"ok": False, "error": "Couldn't save that link."}
    apply_queue.stage(user_id, row["id"])
    return {
        "ok": True,
        "created": created,
        "posting_id": row["id"],
        "title": row["title"] or posting.title,
        "company": row["company"] or posting.company,
        "url": row["url"] or posting.url,
        "source": row["source"],
        "apply_kind": ats.apply_kind(row["url"], row["source"]),
    }
