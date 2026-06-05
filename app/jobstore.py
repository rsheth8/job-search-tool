"""Persistence for job discovery: tracked boards + discovered postings.

Kept separate from ``store.py`` (applications) so the discovery feature is
self-contained. Dedupe rides the ``(user_id, source, external_id)`` unique index
in ``db.py`` — ``save_posting`` is INSERT-OR-IGNORE, so a posting is stored,
scored, and alerted exactly once.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .db import connect
from .jobsources import JobPosting
from . import posting_match


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Tracked boards
# ---------------------------------------------------------------------------

def add_tracked_company(
    user_id: str, source: str, board_token: str, company_name: str | None = None
) -> sqlite3.Row | None:
    """Track a board. Returns the row, or None if it was already tracked."""
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO tracked_companies
                (user_id, source, board_token, company_name, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, source.lower(), board_token, company_name, _now()),
        )
        if cur.rowcount == 0:
            return None
        return conn.execute(
            "SELECT * FROM tracked_companies WHERE id = ?", (cur.lastrowid,)
        ).fetchone()


def list_tracked(user_id: str) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM tracked_companies WHERE user_id = ? ORDER BY company_name, board_token",
            (user_id,),
        ).fetchall()


def remove_tracked(user_id: str, name_or_token: str) -> int:
    """Untrack by display name (case-insensitive) or board token. Rows removed."""
    needle = name_or_token.strip()
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM tracked_companies WHERE user_id = ? "
            "AND (board_token = ? OR LOWER(company_name) = LOWER(?))",
            (user_id, needle, needle),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Postings
# ---------------------------------------------------------------------------

def posting_exists(user_id: str, source: str, external_id: str) -> bool:
    with connect() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM job_postings WHERE user_id = ? AND source = ? AND external_id = ?",
                (user_id, source, external_id),
            ).fetchone()
            is not None
        )


def save_posting(
    user_id: str,
    posting: JobPosting,
    *,
    relevance_score: float | None = None,
    status: str = "new",
) -> sqlite3.Row | None:
    """Insert a discovered posting. Returns the row, or None if it already existed."""
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO job_postings
                (user_id, source, external_id, company, title, location, url,
                 description, posted_at, first_seen_at, relevance_score, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id, posting.source, posting.external_id, posting.company,
                posting.title, posting.location, posting.url, posting.description,
                posting.posted_at, _now(), relevance_score, status,
            ),
        )
        if cur.rowcount == 0:
            return None
        return conn.execute(
            "SELECT * FROM job_postings WHERE id = ?", (cur.lastrowid,)
        ).fetchone()


def has_postings_from_source(user_id: str, source: str) -> bool:
    """True if the user has any posting from ``source`` — used to baseline the
    paid aggregator on its first run (so enabling it doesn't storm)."""
    with connect() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM job_postings WHERE user_id = ? AND source = ? LIMIT 1",
                (user_id, source),
            ).fetchone()
            is not None
        )


def get_posting(user_id: str, posting_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM job_postings WHERE user_id = ? AND id = ?",
            (user_id, posting_id),
        ).fetchone()


def _filter_already_applied(
    user_id: str, rows: list[sqlite3.Row]
) -> list[sqlite3.Row]:
    """Drop postings the user already logged an application for."""
    return [
        r for r in rows
        if not posting_match.user_already_applied_to(
            user_id, r["company"], r["title"]
        )
    ]


def mark_matching_postings_applied(
    user_id: str, company: str, role: str | None
) -> int:
    """Mark open postings that match a newly logged application. Returns count."""
    if not role:
        return 0
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, company, title FROM job_postings
            WHERE user_id = ? AND status IN ('queued', 'alerted', 'new')
            """,
            (user_id,),
        ).fetchall()
    n = 0
    for row in rows:
        if posting_match.matches_application(
            company, role, row["company"], row["title"]
        ):
            mark_posting_status(row["id"], "applied")
            n += 1
    return n


def list_review_queue(user_id: str, *, limit: int = 50) -> list[sqlite3.Row]:
    """Queued matches awaiting interactive review, best score first."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM job_postings
            WHERE user_id = ? AND status = 'queued'
            ORDER BY relevance_score DESC, first_seen_at ASC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return _filter_already_applied(user_id, rows)[:limit]


def count_queued(user_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM job_postings WHERE user_id = ? AND status = 'queued'",
            (user_id,),
        ).fetchone()[0]


def list_postings(
    user_id: str,
    *,
    statuses: tuple[str, ...] | None = None,
    limit: int = 20,
    exclude_already_applied: bool = False,
) -> list[sqlite3.Row]:
    """Most recently seen postings, optionally filtered to given statuses."""
    sql = "SELECT * FROM job_postings WHERE user_id = ? "
    params: list = [user_id]
    if statuses:
        sql += f"AND status IN ({','.join('?' * len(statuses))}) "
        params.extend(statuses)
    # SQLite sorts NULLs last under DESC, so unscored postings sink naturally.
    sql += "ORDER BY relevance_score DESC, first_seen_at DESC LIMIT ?"
    fetch_limit = limit * 3 if exclude_already_applied else limit
    params.append(fetch_limit)
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    if exclude_already_applied:
        rows = _filter_already_applied(user_id, rows)
    return rows[:limit]


def mark_posting_status(posting_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE job_postings SET status = ?, snoozed_until = NULL WHERE id = ?",
            (status, posting_id),
        )


def snooze_posting(posting_id: int, until_iso: str) -> None:
    """Mute a posting until ``until_iso`` — hidden from listings until then,
    when ``wake_snoozed`` resurfaces it as 'queued' (rejoins the review queue)."""
    with connect() as conn:
        conn.execute(
            "UPDATE job_postings SET status = 'snoozed', snoozed_until = ? WHERE id = ?",
            (until_iso, posting_id),
        )


def wake_snoozed(user_id: str, now_iso: str) -> int:
    """Flip expired snoozes back to 'queued'. Returns how many woke."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE job_postings SET status = 'queued', snoozed_until = NULL "
            "WHERE user_id = ? AND status = 'snoozed' AND snoozed_until IS NOT NULL "
            "AND snoozed_until <= ?",
            (user_id, now_iso),
        )
        return cur.rowcount


def dismiss_all_queued(user_id: str) -> int:
    """Mark every queued posting dismissed. Returns rows updated."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE job_postings SET status = 'dismissed' "
            "WHERE user_id = ? AND status = 'queued'",
            (user_id,),
        )
        return cur.rowcount


def counts_by_status(user_id: str) -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM job_postings WHERE user_id = ? GROUP BY status",
            (user_id,),
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}


# ---------------------------------------------------------------------------
# Cross-user helpers (background loop + /health)
# ---------------------------------------------------------------------------

def board_stats(user_id: str) -> list[dict]:
    """Per-tracked-board posting counts for the richer 'what am I tracking' view.

    Postings carry the board's display name (``tick`` sets it), so we match on
    (source, company_name). ``fresh`` = currently surfaced (queued/alerted/new).
    """
    out: list[dict] = []
    with connect() as conn:
        for b in list_tracked(user_id):
            row = conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN status IN ('queued','alerted','new') THEN 1 ELSE 0 END) AS fresh, "
                "MAX(first_seen_at) AS last_seen "
                "FROM job_postings WHERE user_id = ? AND source = ? AND company = ?",
                (user_id, b["source"], b["company_name"]),
            ).fetchone()
            out.append({
                "board": b,
                "total": row["total"] or 0,
                "fresh": row["fresh"] or 0,
                "last_seen": row["last_seen"],
            })
    return out


def all_tracked_users() -> list[str]:
    """Distinct users with at least one tracked board (drives the poll loop)."""
    with connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT user_id FROM tracked_companies ORDER BY user_id"
            )
        ]


def all_discovery_users() -> list[str]:
    """Users who should receive discovery ticks (tracked boards and/or profile)."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT user_id FROM tracked_companies
            UNION
            SELECT user_id FROM job_search_profile
            WHERE COALESCE(roles, '') != ''
               OR COALESCE(keywords, '') != ''
               OR COALESCE(locations, '') != ''
            ORDER BY user_id
            """
        ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Directory cursor + aggregator caps
# ---------------------------------------------------------------------------

_DIRECTORY_CURSOR_KEY = "directory:global"


def get_directory_cursor() -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT position FROM discovery_cursors WHERE cursor_key = ?",
            (_DIRECTORY_CURSOR_KEY,),
        ).fetchone()
    return int(row["position"]) if row else 0


def set_directory_cursor(position: int) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO discovery_cursors (cursor_key, position, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cursor_key) DO UPDATE SET
                position = excluded.position,
                updated_at = excluded.updated_at
            """,
            (_DIRECTORY_CURSOR_KEY, position, _now()),
        )


def _utc_day_start() -> str:
    from datetime import datetime, timezone

    d = datetime.now(timezone.utc).date()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat()


def allow_aggregator_search() -> bool:
    from .config import get_settings

    cap = get_settings().job_aggregator_max_per_day
    with connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM job_api_calls WHERE call_type = 'aggregator' "
            "AND called_at >= ?",
            (_utc_day_start(),),
        ).fetchone()[0]
    return n < cap


def record_aggregator_call(user_id: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO job_api_calls (call_type, user_id, called_at) VALUES (?, ?, ?)",
            ("aggregator", user_id, _now()),
        )


def tracked_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM tracked_companies").fetchone()[0]


def global_counts_by_status() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM job_postings GROUP BY status"
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}
