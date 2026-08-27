"""The user's job-search profile: what they want + a short resume summary.

Drives (1) the free keyword/location pre-filter and LLM relevance scoring in
``matcher.py`` and (2) assisted-apply drafts in Phase 2. One row per user,
upserted field-by-field so partial updates ("just set my locations") don't wipe
the rest.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .db import connect

# Columns a caller may set; user_id/updated_at are managed here.
_FIELDS = (
    "roles", "keywords", "locations", "seniority", "resume_summary",
    "prefs_json", "min_relevance", "applicant_json",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_profile(user_id: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM job_search_profile WHERE user_id = ?", (user_id,)
        ).fetchone()


def has_profile(user_id: str) -> bool:
    row = get_profile(user_id)
    if row is None:
        return False
    # A row with every matching field empty is effectively "no profile yet".
    return any((row[f] or "").strip() for f in ("roles", "keywords", "locations"))


def public_fields(user_id: str) -> dict:
    """Search-profile fields the iOS setup wizard reads/writes."""
    keys = ("roles", "keywords", "locations", "seniority")
    row = get_profile(user_id)
    if row is None:
        return {k: "" for k in keys}
    return {k: (row[k] or "") for k in keys}


def all_profile_users() -> list[str]:
    """Every user with a saved profile (drives aggregator-only discovery sweeps)."""
    with connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT user_id FROM job_search_profile ORDER BY user_id"
            )
        ]


def set_profile(user_id: str, **fields) -> sqlite3.Row:
    """Upsert the given fields. Unknown keys are ignored; unset fields untouched."""
    updates = {k: v for k, v in fields.items() if k in _FIELDS and v is not None}
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM job_search_profile WHERE user_id = ?", (user_id,)
        ).fetchone()
        if exists is None:
            cols = ["user_id", "updated_at", *updates.keys()]
            vals = [user_id, _now(), *updates.values()]
            conn.execute(
                f"INSERT INTO job_search_profile ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                vals,
            )
        elif updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE job_search_profile SET {sets}, updated_at = ? WHERE user_id = ?",
                [*updates.values(), _now(), user_id],
            )
        else:
            conn.execute(
                "UPDATE job_search_profile SET updated_at = ? WHERE user_id = ?",
                (_now(), user_id),
            )
        return conn.execute(
            "SELECT * FROM job_search_profile WHERE user_id = ?", (user_id,)
        ).fetchone()


def set_min_relevance(user_id: str, value: float | None) -> None:
    """Set (or clear, with None) the per-user alert threshold.

    ``set_profile`` skips None values, so clearing back to the global default
    needs this explicit NULL write. Upserts so a threshold can be set before any
    roles are saved.
    """
    with connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM job_search_profile WHERE user_id = ?", (user_id,)
        ).fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO job_search_profile (user_id, min_relevance, updated_at) "
                "VALUES (?, ?, ?)",
                (user_id, value, _now()),
            )
        else:
            conn.execute(
                "UPDATE job_search_profile SET min_relevance = ?, updated_at = ? "
                "WHERE user_id = ?",
                (value, _now(), user_id),
            )


def effective_threshold(row: sqlite3.Row | None, default: float) -> float:
    """The alert threshold for a user: their saved ``min_relevance`` or ``default``."""
    if row is None:
        return default
    try:
        val = row["min_relevance"]
    except (IndexError, KeyError):
        return default
    return float(val) if val is not None else default


def profile_text(row: sqlite3.Row | None) -> str:
    """Render a profile as a compact block for LLM scoring / display."""
    if row is None:
        return ""
    parts = []
    for label, field in (
        ("Roles", "roles"),
        ("Keywords", "keywords"),
        ("Locations", "locations"),
        ("Seniority", "seniority"),
        ("Background", "resume_summary"),
    ):
        val = (row[field] or "").strip()
        if val:
            parts.append(f"{label}: {val}")
    return "\n".join(parts)
