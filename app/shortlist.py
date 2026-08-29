"""Rank matches for "apply these today" — real, fresh, fillable first.

Fit score answers "is this you?". This layer answers "should you spend an
application on it *today*?": company ATS you can autofill, posted in the last
48 hours, not a bumped evergreen. Used to order the discovery cap, the digest,
and the Apply-tab queue. Never mutates the matcher's fit score.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from . import ats
from .jobsources import quality
from .jobsources.base import parse_posted_at

FRESH_HOURS = 48
_STALE_DAYS = 45
_AGE_RE = re.compile(r"(\d+)\s*\+?\s*days?\s*ago", re.I)
_AGE_WEEKS_RE = re.compile(r"(\d+)\s*\+?\s*weeks?\s*ago", re.I)
_AGE_MONTHS_RE = re.compile(r"(\d+)\s*\+?\s*months?\s*ago", re.I)


def _field(obj, key, default=None):
    if obj is None:
        return default
    if hasattr(obj, key):
        value = getattr(obj, key, default)
        if value is not None:
            return value
    try:
        value = obj[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def age_days(posted_at: str | None, *, now: datetime | None = None) -> float | None:
    """Best-effort age in days. None when we can't tell."""
    if not posted_at:
        return None
    text = posted_at.strip()
    if not text:
        return None
    for rx, per_unit in ((_AGE_RE, 1), (_AGE_WEEKS_RE, 7), (_AGE_MONTHS_RE, 30)):
        m = rx.search(text)
        if m:
            try:
                return float(int(m.group(1)) * per_unit)
            except ValueError:
                return None
    dt = parse_posted_at(text)
    if dt is None:
        return None
    stamp = now or datetime.now(timezone.utc)
    return max(0.0, (stamp - dt).total_seconds() / 86400.0)


def is_fresh(posted_at: str | None, *, hours: float = FRESH_HOURS,
             now: datetime | None = None) -> bool:
    days = age_days(posted_at, now=now)
    if days is None:
        return False
    return days <= hours / 24.0


def freshness_score(posted_at: str | None, *, now: datetime | None = None) -> float:
    """1.0 if posted in the last 48h, decaying to 0 by ~45 days. Unknown → 0.3."""
    days = age_days(posted_at, now=now)
    if days is None:
        return 0.3
    if days <= 2:
        return 1.0
    if days >= _STALE_DAYS:
        return 0.0
    return max(0.0, 1.0 - (days - 2) / (_STALE_DAYS - 2))


def rank_tuple(obj, *, score: float | None = None,
               now: datetime | None = None) -> tuple:
    """Sort key, larger first: fillable, first-party, fresh, fit, newer."""
    url = _field(obj, "url", "") or ""
    source = (_field(obj, "source", "") or "").lower()
    posted = _field(obj, "posted_at", "") or ""
    fillable = 1 if ats.is_fillable_form(url) else 0
    first = 1 if source in quality.FIRST_PARTY_SOURCES else 0
    fresh = 1 if is_fresh(posted, now=now) else 0
    if score is None:
        try:
            score = float(_field(obj, "relevance_score", 0) or 0)
        except (TypeError, ValueError):
            score = 0.0
    age = age_days(posted, now=now)
    newer = -(age if age is not None else 9999.0)
    return (fillable, first, fresh, float(score or 0), newer)


def discovery_sort_key(posting, *, now: datetime | None = None) -> tuple:
    """Pre-score cap order: fillable + fresh first so today's ATS drops get scored."""
    return rank_tuple(posting, score=0.0, now=now)


def rank_rows(rows: list, *, now: datetime | None = None) -> list:
    """Queued store rows: user pin order first, then apply-today order."""
    pinned = [r for r in rows if _field(r, "sort_order") is not None]
    auto = [r for r in rows if _field(r, "sort_order") is None]
    pinned.sort(key=lambda r: int(_field(r, "sort_order") or 0))
    auto.sort(key=lambda r: rank_tuple(r, now=now), reverse=True)
    return pinned + auto


def rank_scored(matches: list[tuple], *, now: datetime | None = None) -> list[tuple]:
    """``(posting, score, id)`` digest order."""
    return sorted(
        matches,
        key=lambda m: rank_tuple(m[0], score=m[1], now=now),
        reverse=True,
    )
