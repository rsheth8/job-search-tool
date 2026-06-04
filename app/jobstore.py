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


def remove_tracked(user_id: str, board_token: str) -> int:
    """Untrack by board token (any source). Returns rows removed."""
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM tracked_companies WHERE user_id = ? AND board_token = ?",
            (user_id, board_token),
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


def get_posting(user_id: str, posting_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM job_postings WHERE user_id = ? AND id = ?",
            (user_id, posting_id),
        ).fetchone()


def list_postings(
    user_id: str, *, statuses: tuple[str, ...] | None = None, limit: int = 20
) -> list[sqlite3.Row]:
    """Most recently seen postings, optionally filtered to given statuses."""
    sql = "SELECT * FROM job_postings WHERE user_id = ? "
    params: list = [user_id]
    if statuses:
        sql += f"AND status IN ({','.join('?' * len(statuses))}) "
        params.extend(statuses)
    # SQLite sorts NULLs last under DESC, so unscored postings sink naturally.
    sql += "ORDER BY relevance_score DESC, first_seen_at DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def mark_posting_status(posting_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE job_postings SET status = ? WHERE id = ?", (status, posting_id)
        )


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

def all_tracked_users() -> list[str]:
    """Distinct users with at least one tracked board (drives the poll loop)."""
    with connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT user_id FROM tracked_companies ORDER BY user_id"
            )
        ]


def tracked_count() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM tracked_companies").fetchone()[0]


def global_counts_by_status() -> dict[str, int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM job_postings GROUP BY status"
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}
