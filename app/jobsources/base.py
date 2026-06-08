"""Shared types + helpers for job-source adapters."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("jobsources")

HTTP_TIMEOUT_SECONDS = 12.0
# Cap stored/scored description length — keeps SQLite small and LLM tokens cheap.
MAX_DESCRIPTION_CHARS = 1500


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
    posted_at: str = ""       # ISO 8601 string, best-effort
    # Semantic vector (Matching v2). Set during scoring when embeddings are
    # active, then persisted by ``jobstore.save_posting`` (embed once, like the
    # score-once dedupe rule). Not part of identity, so excluded from dedupe.
    embedding: list[float] | None = field(default=None, compare=False)

    def dedupe_key(self) -> tuple[str, str]:
        return (self.source, self.external_id)


# ---------------------------------------------------------------------------
# HTTP — one resilient JSON GET shared by every adapter
# ---------------------------------------------------------------------------

def get_json(url: str):
    """GET ``url`` and return parsed JSON, or ``None`` on any failure.

    Never raises: network errors, non-2xx, and bad JSON all log and return None
    so a single bad board can't take down a discovery tick.
    """
    import httpx  # lazy: offline/test paths never import it

    try:
        resp = httpx.get(
            url,
            timeout=HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"Accept": "application/json", "User-Agent": "job-search-tool/1.0"},
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:  # noqa: BLE001 — adapters degrade to [] on any error
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
