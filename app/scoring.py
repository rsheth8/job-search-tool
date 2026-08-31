"""Follow-up prioritization.

Turns the recency-only QUERY into a real "what should I chase?" ranking.

Score (per the spec):  days_since_last_activity * 2  +  stage_weight  +  recruiter_bonus

- days_since_last_activity uses ``last_updated_at`` rather than ``applied_at``:
  an application you spoke to yesterday doesn't need a nudge, one that's gone
  quiet does. This is the staleness signal that actually drives follow-ups.
- stage_weight rewards later stages — a pending Onsite is more worth chasing
  than a fresh application.
- recruiter_bonus fires when notes (or a legacy recruiters row) signal a
  recruiter for the company.

Pure functions here (easy to test offline); orchestration that touches the DB
lives in ``rank_followups``.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import store

# Terminal-ish statuses never surface as follow-ups.
TERMINAL_STATUSES = {"Offer", "Rejected", "Ghosted"}

# Later stages are closer to an offer → higher priority to keep warm.
STAGE_WEIGHTS: dict[str, float] = {
    "Applied": 1.0,
    "OA received": 3.0,
    "Phone screen": 4.0,
    "Interview": 5.0,
    "Onsite": 6.0,
}

DAYS_WEIGHT = 2.0
RECRUITER_BONUS = 2.0


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def days_since(iso: str | None, now: datetime) -> float:
    dt = _parse(iso)
    if dt is None:
        return 0.0
    return max(0.0, (now - dt).total_seconds() / 86400.0)


def score_application(
    app: sqlite3.Row | dict,
    *,
    now: datetime,
    has_recruiter: bool = False,
) -> tuple[float, dict]:
    """Return (score, breakdown). Breakdown is for explainability/tests."""
    stale_days = days_since(app["last_updated_at"], now)
    days_component = stale_days * DAYS_WEIGHT
    stage_component = STAGE_WEIGHTS.get(app["status"], 1.0)
    recruiter_component = RECRUITER_BONUS if has_recruiter else 0.0
    total = days_component + stage_component + recruiter_component
    breakdown = {
        "stale_days": round(stale_days, 2),
        "days_component": round(days_component, 2),
        "stage_component": stage_component,
        "recruiter_component": recruiter_component,
        "total": round(total, 2),
    }
    return total, breakdown


def rank_followups(
    user_id: str, *, now: datetime | None = None, limit: int = 50
) -> list[tuple[sqlite3.Row, float, dict]]:
    """Open applications for a user, highest follow-up priority first."""
    now = now or datetime.now(timezone.utc)
    apps = store.list_applications(user_id, limit=limit)
    ranked: list[tuple[sqlite3.Row, float, dict]] = []
    for a in apps:
        if a["status"] in TERMINAL_STATUSES:
            continue
        has_recruiter = store.has_recruiter_signal(user_id, a["id"])
        total, breakdown = score_application(
            a, now=now, has_recruiter=has_recruiter
        )
        ranked.append((a, total, breakdown))
    ranked.sort(key=lambda t: t[1], reverse=True)
    return ranked
