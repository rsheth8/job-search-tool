"""Shared types + helpers for job-source adapters."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("jobsources")

HTTP_TIMEOUT_SECONDS = 12.0
# Cap stored description length. Generous so matchers get the real
# responsibilities/requirements (JDs front-load boilerplate); still bounded to
# keep SQLite small. LLM callers slice this further for token cost.
MAX_DESCRIPTION_CHARS = 3000


@dataclass
class JobPosting:
    """One open role from a job board, normalized across sources."""

    source: str
    external_id: str          # stable id from the board (deduped on this)
    title: str
    url: str
    company: str = ""
    location: str = ""
    description: str = ""
    posted_at: str = ""       # ISO 8601 first-published when known (not last bump)
    updated_at: str = ""      # last ATS bump, if distinct from posted_at
    # Legacy embedding column support (unused). Kept for DB schema compat.
    embedding: list[float] | None = field(default=None, compare=False)

    def dedupe_key(self) -> tuple[str, str]:
        return (self.source, self.external_id)


# ---------------------------------------------------------------------------
# HTTP — one resilient JSON GET shared by every adapter
# ---------------------------------------------------------------------------

def get_json(url: str, *, timeout: float | None = None):
    """GET ``url`` and return parsed JSON, or ``None`` on any failure.

    Never raises: network errors, non-2xx, and bad JSON all log and return None
    so a single bad board can't take down a discovery tick.
    """
    import httpx  # lazy: offline/test paths never import it

    try:
        resp = httpx.get(
            url,
            timeout=timeout or HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"Accept": "application/json", "User-Agent": "job-search-tool/1.0"},
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:  # noqa: BLE001 — adapters degrade to [] on any error
        logger.warning("job source fetch failed: %s", url, exc_info=True)
        return None


def get_text(url: str, *, timeout: float | None = None, accept: str = "*/*") -> str | None:
    """GET ``url`` and return response text, or ``None`` on any failure."""
    import httpx

    try:
        resp = httpx.get(
            url,
            timeout=timeout or HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"Accept": accept, "User-Agent": "job-search-tool/1.0"},
        )
        resp.raise_for_status()
        return resp.text
    except Exception:  # noqa: BLE001
        logger.warning("job source fetch failed: %s", url, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str | None) -> str:
    """Crude HTML -> text: unescape entities, drop tags, collapse, truncate.

    Greenhouse entity-encodes its ``content`` (e.g. ``&lt;p&gt;``), so unescape
    first, then strip tags.
    """
    if not text:
        return ""
    import html

    cleaned = _WS_RE.sub(" ", _TAG_RE.sub(" ", html.unescape(text))).strip()
    if len(cleaned) > MAX_DESCRIPTION_CHARS:
        cleaned = cleaned[:MAX_DESCRIPTION_CHARS].rstrip() + "…"
    return cleaned


def iso_from_epoch_ms(value) -> str:
    """Lever gives epoch-millis; normalize to an ISO string. Best-effort."""
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def parse_posted_at(value: str | None) -> datetime | None:
    """Parse an ISO posted/updated stamp. None on missing or garbage input."""
    text = (value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def first_published(published: str | None, updated: str | None) -> tuple[str, str]:
    """``(posted_at, updated_at)`` preferring first-published over a last bump."""
    pub = (published or "").strip()
    upd = (updated or "").strip()
    if pub:
        return pub, upd
    return upd, ""
