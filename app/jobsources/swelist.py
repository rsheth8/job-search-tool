"""Pitt CSC / Simplify internship list (github.com/SimplifyJobs/Summer2027-Internships).

The README APPLY buttons often wrap Simplify's tracker. The structured source of
truth is ``.github/scripts/listings.json`` — each row already has a company ATS
``url`` (Greenhouse, Lever, Workday, …). We poll that JSON, keep **active +
visible + recent** rows, and unwrap the few remaining proxy URLs (Simplify's
``internshiplist2000`` Greenhouse board, simplify.jobs) by following redirects
so the stored link is the real application when we can find it.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from .base import HTTP_TIMEOUT_SECONDS, USER_AGENT, JobPosting, get_json, iso_from_epoch_ms

logger = logging.getLogger("jobsources.swelist")

DEFAULT_LIST = "summer2027"

LISTS: dict[str, dict[str, str]] = {
    "summer2027": {
        "url": (
            "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/"
            "dev/.github/scripts/listings.json"
        ),
        "label": "Pitt CSC / Simplify Summer 2027 internships",
        "repo": "https://github.com/SimplifyJobs/Summer2027-Internships",
        "kind": "internship",
    },
    "newgrad": {
        "url": (
            "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/"
            "dev/.github/scripts/listings.json"
        ),
        "label": "Pitt CSC / Simplify new-grad positions",
        "repo": "https://github.com/SimplifyJobs/New-Grad-Positions",
        "kind": "newgrad",
    },
}

# Apply links that are Simplify's own board / tracker, not the company's ATS.
_PROXY_HOST_RE = re.compile(
    r"internshiplist2000|simplify\.jobs|(?:^|\.)simplify\.com$",
    re.I,
)

_UNWRAP_CAP = 12


def list_ids() -> list[str]:
    return list(LISTS.keys())


def resolve_list(token: str) -> dict | None:
    key = (token or "").strip().lower() or DEFAULT_LIST
    if key in LISTS:
        return {"list_id": key, **LISTS[key]}
    if token.startswith("http://") or token.startswith("https://"):
        return {
            "list_id": "custom",
            "url": token.strip(),
            "label": "Custom internship listings JSON",
            "repo": "",
        }
    return None


def is_proxy_apply_url(url: str) -> bool:
    """True when ``url`` is Simplify's tracker/board rather than a company ATS."""
    raw = (url or "").strip()
    if not raw:
        return True
    host = urlparse(raw).netloc.lower()
    path = urlparse(raw).path.lower()
    blob = f"{host}{path}"
    return bool(_PROXY_HOST_RE.search(blob) or _PROXY_HOST_RE.search(host))


def _iso_from_unix(value) -> str:
    """listings.json uses unix seconds, not Lever's millis."""
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return ""
    if ts > 10_000_000_000:  # already millis
        return iso_from_epoch_ms(ts)
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return ""


def _is_fresh(listing: dict, max_age_days: int, now: datetime) -> bool:
    if max_age_days <= 0:
        return True
    ts = listing.get("date_updated") or listing.get("date_posted")
    try:
        epoch = int(ts)
    except (TypeError, ValueError):
        return True  # undated: keep; might be a brand-new drop
    if epoch > 10_000_000_000:
        epoch //= 1000
    posted = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return (now - posted).total_seconds() <= max_age_days * 86400


def configured_list_ids() -> list[str]:
    """List ids from ``JOB_SWELIST_LIST`` (comma-separated), known ids only."""
    from ..config import get_settings

    raw = (get_settings().job_swelist_list or DEFAULT_LIST).strip()
    out: list[str] = []
    for part in raw.split(","):
        key = part.strip().lower()
        if key in LISTS and key not in out:
            out.append(key)
    return out or [DEFAULT_LIST]


def _kind(list_id: str) -> str:
    return (LISTS.get(list_id) or {}).get("kind") or "internship"


def _description(listing: dict, *, list_id: str = DEFAULT_LIST) -> str:
    kind = _kind(list_id)
    if kind == "newgrad":
        bits = ["New-grad role from the Pitt CSC / Simplify list."]
    else:
        bits = ["Internship from the Pitt CSC / Simplify list."]
    cat = (listing.get("category") or "").strip()
    if cat:
        bits.append(f"Category: {cat}.")
    terms = listing.get("terms") or []
    if isinstance(terms, list) and terms:
        bits.append("Terms: " + ", ".join(str(t) for t in terms if t) + ".")
    sponsor = (listing.get("sponsorship") or "").strip()
    if sponsor and sponsor.lower() not in ("other", "n/a", "na", ""):
        bits.append(f"Sponsorship: {sponsor}.")
    degrees = listing.get("degrees") or []
    if isinstance(degrees, list) and degrees:
        bits.append("Degrees: " + ", ".join(str(d) for d in degrees if d) + ".")
    if is_proxy_apply_url(str(listing.get("url") or "")):
        bits.append("Apply URL may be a Simplify proxy — confirm the company careers page.")
    return " ".join(bits)


def _parse(
    data,
    *,
    list_id: str,
    max_age_days: int = 21,
    now: datetime | None = None,
) -> list[JobPosting]:
    """Pure parse of listings.json. Never hits the network."""
    if not isinstance(data, list):
        return []
    now = now or datetime.now(timezone.utc)
    out: list[JobPosting] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        if raw.get("active") is False or raw.get("is_visible") is False:
            continue
        url = str(raw.get("url") or "").strip()
        title = str(raw.get("title") or "").strip()
        company = str(raw.get("company_name") or "").strip()
        ext = str(raw.get("id") or "").strip()
        if not (url.startswith("http") and title and company and ext):
            continue
        if not _is_fresh(raw, max_age_days, now):
            continue
        locs = raw.get("locations") or []
        location = ", ".join(str(x) for x in locs if x) if isinstance(locs, list) else str(locs)
        out.append(
            JobPosting(
                source="swelist",
                external_id=f"{list_id}:{ext}",
                title=title,
                url=url,
                company=company,
                location=location,
                description=_description(raw, list_id=list_id),
                posted_at=_iso_from_unix(raw.get("date_posted") or raw.get("date_updated")),
            )
        )
    # Newest first so the per-tick scoring cap sees fresh drops, not the backlog.
    out.sort(key=lambda p: p.posted_at or "", reverse=True)
    return out


def _unwrap_apply_url(url: str) -> str:
    """Follow redirects off Simplify's proxy onto a company ATS when possible."""
    if not is_proxy_apply_url(url):
        return url
    import httpx

    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    try:
        resp = httpx.get(
            url,
            timeout=HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers=headers,
        )
        final = str(resp.url)
        if final.startswith("http") and not is_proxy_apply_url(final):
            return final
    except Exception:  # noqa: BLE001 — keep the original listing URL
        logger.debug("swelist unwrap failed for %s", url, exc_info=True)
    return url


def fetch(token: str) -> list[JobPosting]:
    """Fetch active recent listings. ``token`` is a list id, comma-separated
    ids, or a raw JSON URL."""
    from ..config import get_settings

    parts = [p.strip() for p in (token or "").split(",") if p.strip()]
    if len(parts) > 1:
        seen: set[str] = set()
        merged: list[JobPosting] = []
        for part in parts:
            for p in fetch(part):
                if p.external_id not in seen:
                    seen.add(p.external_id)
                    merged.append(p)
        merged.sort(key=lambda p: p.posted_at or "", reverse=True)
        return merged

    meta = resolve_list(token)
    if not meta:
        logger.warning("unknown swelist token %r", token)
        return []
    # New-grad listings.json is large; give the download a bit more time.
    data = get_json(meta["url"], timeout=45.0)
    if data is None:
        return []
    settings = get_settings()
    posts = _parse(
        data,
        list_id=meta["list_id"],
        max_age_days=max(0, int(settings.job_swelist_max_age_days)),
    )
    unwrapped = 0
    for p in posts:
        if unwrapped >= _UNWRAP_CAP:
            break
        if is_proxy_apply_url(p.url):
            nxt = _unwrap_apply_url(p.url)
            if nxt != p.url:
                p.url = nxt
                unwrapped += 1
    logger.info(
        "swelist %s: %d active recent posting(s) (unwrapped %d proxy url(s))",
        meta["list_id"], len(posts), unwrapped,
    )
    return posts
