"""Confirm an apply URL is still an open req before we surface it.

Discovery can pull thousands of RSS items; this only runs on the capped
shortlist (tens per tick) and the Apply tab's top few. Fail-open: a network
error or login wall never buries a role. 404, a JSON "gone" from the ATS, a
redirect off the job, or obvious closed-page copy do drop it.

Greenhouse / Lever / SmartRecruiters expose a public JSON job endpoint that
404s when the req is taken down — that is the source of truth. HTML is the
fallback (Ashby, Workable, RSS) and is matched after tags are stripped.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import urlparse

from .. import ats
from . import ghost
from .base import USER_AGENT, JobPosting

logger = logging.getLogger("jobsources.alive")

_TIMEOUT = 4.0
_BODY_CHARS = 8000
_CLOSED_STATUSES = {404, 410, 451}
_FAIL_OPEN_STATUSES = {401, 403, 407, 429, 500, 502, 503, 504}
_CACHE_TTL_SEC = 6 * 3600
_LOGIN_MARKERS = (
    "login.", "logon.", "signin.", "sign-in.", "okta.com", "auth0.com",
    "microsoftonline.", "accounts.google.", "idp.",
)

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, bool]] = {}  # url -> (expires_at, is_open)


@dataclass(frozen=True)
class FetchResult:
    status: int
    text: str
    url: str = ""  # final URL after redirects


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _cache_get(url: str) -> bool | None:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(url)
        if hit is None:
            return None
        expires, is_open = hit
        if expires < now:
            _cache.pop(url, None)
            return None
        return is_open


def _cache_put(url: str, is_open: bool) -> None:
    with _cache_lock:
        _cache[url] = (time.monotonic() + _CACHE_TTL_SEC, is_open)


def http_get(url: str, *, timeout: float = _TIMEOUT) -> FetchResult:
    """GET ``url``. Callers treat a failed fetch as fail-open."""
    import httpx

    resp = httpx.get(
        url,
        timeout=timeout,
        follow_redirects=True,
        headers={
            "Accept": "application/json, text/html, */*",
            "User-Agent": USER_AGENT,
        },
    )
    text = (resp.text or "")[:_BODY_CHARS]
    return FetchResult(resp.status_code, text, str(resp.url or url))


def inspect_apply_url(
    url: str, *, timeout: float = _TIMEOUT, get=None, use_cache: bool = True
) -> tuple[bool, str]:
    """``(is_open, reason)``. Fail-open on fetch errors.

    ``get(url, timeout=)`` is injectable for tests and must return ``FetchResult``.
    """
    raw = (url or "").strip()
    if not raw:
        return True, "empty"
    if use_cache:
        cached = _cache_get(raw)
        if cached is not None:
            return cached, "cache"
    try:
        is_open, reason = _inspect(raw, timeout=timeout, get=get or http_get)
    except Exception:  # noqa: BLE001 — never fail a tick on a flaky GET
        logger.info("alive: fail-open on error for %s", raw, exc_info=True)
        return True, "error"
    if use_cache:
        _cache_put(raw, is_open)
    return is_open, reason


def check_apply_url(url: str, *, timeout: float = _TIMEOUT, get=None,
                    request=None, use_cache: bool = True) -> bool:
    """True if the apply URL still looks open. Fail-open on any fetch error.

    ``request(method, url)`` is a legacy test seam: returns ``(status, text)``.
    Prefer ``get``.
    """
    getter = get
    if getter is None and request is not None:
        def getter(u, timeout=_TIMEOUT):  # noqa: ARG001
            packed = request("GET", u)
            if isinstance(packed, FetchResult):
                return packed
            if isinstance(packed, tuple) and len(packed) == 3:
                status, text, final = packed
                return FetchResult(int(status), text or "", final or u)
            status, text = packed
            return FetchResult(int(status), text or "", u)
    is_open, _reason = inspect_apply_url(
        url, timeout=timeout, get=getter, use_cache=use_cache
    )
    return is_open


def _inspect(url: str, *, timeout: float, get) -> tuple[bool, str]:
    probe = ats.json_probe_url(url)
    if probe:
        result = None
        try:
            try:
                result = get(probe, timeout=timeout)
            except TypeError:
                result = get(probe)
        except Exception:
            result = None
        if result is not None:
            decided = _json_verdict(ats.ats_of(url) or "", result)
            if decided is not None:
                return decided
        # JSON unknown / down → HTML fallback

    try:
        result = get(url, timeout=timeout)
    except TypeError:
        result = get(url)
    status = result.status
    if status in _CLOSED_STATUSES:
        return False, f"http_{status}"
    if status in _FAIL_OPEN_STATUSES or status >= 500:
        return True, f"http_{status}_open"
    if _looks_like_login(result.url or url):
        return True, "login_wall"
    if _redirected_off_job(url, result.url or ""):
        return False, "redirected"
    if ghost.page_says_closed(result.text):
        return False, "closed_copy"
    return True, "open"


def _json_verdict(source: str, result: FetchResult) -> tuple[bool, str] | None:
    if result.status in _CLOSED_STATUSES:
        return False, "json_404"
    if result.status in _FAIL_OPEN_STATUSES or result.status >= 500:
        return None
    if result.status != 200:
        return None
    live = _json_is_live_job(source, result.text)
    if live is True:
        return True, "json_live"
    if live is False:
        return False, "json_dead"
    return None


def _json_is_live_job(source: str, text: str) -> bool | None:
    """True = live req, False = gone, None = don't know (try HTML)."""
    try:
        data = json.loads(text or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    src = (source or "").lower()
    status = str(data.get("status") or data.get("state") or "").lower()
    if status in ("closed", "filled", "expired", "archived", "deleted"):
        return False
    if src == "greenhouse":
        if data.get("id") and (data.get("title") or data.get("absolute_url")):
            return True
        return None
    if src == "lever":
        if data.get("id") and (data.get("text") or data.get("hostedUrl") or data.get("applyUrl")):
            return True
        return None
    if src == "smartrecruiters":
        if data.get("id") and (data.get("name") or data.get("uuid")):
            return True
        return None
    return None


def _looks_like_login(final_url: str) -> bool:
    raw = (final_url or "").lower()
    host = (urlparse(final_url or "").hostname or "").lower()
    hay = f"{host} {raw}"
    return any(m in hay for m in _LOGIN_MARKERS)


def _redirected_off_job(original: str, final: str) -> bool:
    """Same ATS host, job id vanished from the path — typical closed GH listing."""
    ref = ats.posting_ref(original)
    if ref is None or not final:
        return False
    source, _token, job_id = ref
    if not job_id:
        return False
    if not ats.same_ats_host(final, source):
        return False
    needle = job_id.lower()
    if needle in final.lower():
        return False
    orig = original.lower()
    if needle not in orig:
        return False
    return True


def filter_open(
    postings: list[JobPosting], *, check=None, workers: int = 8
) -> tuple[list[JobPosting], int]:
    """Return (kept, dropped_count). Fail-open per URL."""
    check = check or check_apply_url
    if not postings:
        return [], 0

    def _safe(p: JobPosting) -> bool:
        try:
            return bool(check(p.url or ""))
        except Exception:  # noqa: BLE001
            return True

    if workers <= 1 or len(postings) <= 2:
        flags = [_safe(p) for p in postings]
    else:
        n = min(workers, len(postings))
        with ThreadPoolExecutor(max_workers=n) as pool:
            flags = list(pool.map(_safe, postings))
    kept = [p for p, ok in zip(postings, flags) if ok]
    return kept, len(postings) - len(kept)


def close_dead_shortlist(user_id: str, *, today_n: int = 5, get=None) -> int:
    """Re-check staged + apply-today URLs. Mark closed ones so they leave Apply.

    Returns how many were closed. Fail-open: a fetch error keeps the row.
    """
    from .. import apply_queue, jobstore

    staged = apply_queue.list_queue(user_id)
    staged_ids = {it["posting_id"] for it in staged}
    queued = [r for r in jobstore.list_review_queue(user_id) if r["id"] not in staged_ids]
    targets: list[tuple[int, str]] = []
    seen: set[int] = set()
    for it in staged:
        pid, url = it["posting_id"], (it.get("url") or "")
        if pid in seen:
            continue
        seen.add(pid)
        targets.append((pid, url))
    for r in queued[: max(0, today_n)]:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        targets.append((r["id"], r["url"] or ""))

    closed = 0
    for pid, url in targets:
        if check_apply_url(url, get=get):
            continue
        jobstore.mark_posting_status(user_id, pid, "closed")
        apply_queue.remove(user_id, pid)
        closed += 1
        logger.info("alive: closed posting %s (%s)", pid, url)
    return closed
