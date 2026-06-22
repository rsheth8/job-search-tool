"""Fill-request lifecycle for the Phase 2 submit pipeline.

A *fill request* is the user asking the headless worker to fill (and, after they
approve the filled preview, submit) one application. The state machine:

    pending     created by the user; waiting for a worker
    filling     a worker has claimed it and is filling the form
    preview     filled; a screenshot + field summary await the user's approval
    approved    the user approved the preview — the worker may submit
    submitting  the worker is submitting
    submitted   done
    failed      something went wrong (error recorded)

**Nothing is ever submitted without an explicit approval** — `preview -> approved`
is a user action, and the worker only submits an `approved` request.

Pure data layer (no browser, no network): the worker drives it through the API.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .db import connect

PENDING, FILLING, PREVIEW, APPROVED, SUBMITTING, SUBMITTED, FAILED = (
    "pending", "filling", "preview", "approved", "submitting", "submitted", "failed"
)
_ACTIVE = (PENDING, FILLING, PREVIEW, APPROVED, SUBMITTING)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create(user_id: str, posting_id: int) -> sqlite3.Row:
    """Open a fill request for a posting. Idempotent while one is already active —
    returns the existing active request instead of duplicating."""
    with connect() as conn:
        existing = conn.execute(
            f"SELECT * FROM fill_requests WHERE user_id = ? AND posting_id = ? "
            f"AND status IN ({','.join('?' * len(_ACTIVE))}) "
            "ORDER BY id DESC LIMIT 1",
            (user_id, posting_id, *_ACTIVE),
        ).fetchone()
        if existing is not None:
            return existing
        cur = conn.execute(
            "INSERT INTO fill_requests (user_id, posting_id, status, created_at, updated_at) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (user_id, posting_id, _now(), _now()),
        )
        return conn.execute(
            "SELECT * FROM fill_requests WHERE id = ?", (cur.lastrowid,)
        ).fetchone()


def claim_next() -> sqlite3.Row | None:
    """Atomically claim the oldest pending request for the worker (pending ->
    filling). None if the queue is empty."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM fill_requests WHERE status = 'pending' "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE fill_requests SET status = 'filling', updated_at = ? WHERE id = ?",
            (_now(), row["id"]),
        )
        return conn.execute(
            "SELECT * FROM fill_requests WHERE id = ?", (row["id"],)
        ).fetchone()


def set_preview(request_id: int, preview: dict) -> bool:
    """Worker reports the filled form (screenshot + field summary); filling ->
    preview, awaiting the user's approval."""
    return _advance(request_id, FILLING, PREVIEW, preview_json=json.dumps(preview))


def approve(user_id: str, request_id: int) -> bool:
    """User approves the preview; preview -> approved. The worker may now submit.
    Scoped to the owner."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE fill_requests SET status = 'approved', updated_at = ? "
            "WHERE id = ? AND user_id = ? AND status = 'preview'",
            (_now(), request_id, user_id),
        )
        return cur.rowcount > 0


def claim_approved() -> sqlite3.Row | None:
    """Worker claims an approved request to submit (approved -> submitting)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM fill_requests WHERE status = 'approved' "
            "ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE fill_requests SET status = 'submitting', updated_at = ? WHERE id = ?",
            (_now(), row["id"]),
        )
        return conn.execute(
            "SELECT * FROM fill_requests WHERE id = ?", (row["id"],)
        ).fetchone()


def mark_submitted(request_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE fill_requests SET status = 'submitted', updated_at = ? WHERE id = ?",
            (_now(), request_id),
        )
        return cur.rowcount > 0


def mark_failed(request_id: int, error: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE fill_requests SET status = 'failed', error = ?, updated_at = ? "
            "WHERE id = ?",
            (error[:500], _now(), request_id),
        )
        return cur.rowcount > 0


def cancel(user_id: str, request_id: int) -> bool:
    """User cancels their own non-terminal request."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE fill_requests SET status = 'failed', error = 'cancelled', "
            "updated_at = ? WHERE id = ? AND user_id = ? AND status NOT IN "
            "('submitted', 'failed')",
            (_now(), request_id, user_id),
        )
        return cur.rowcount > 0


def get(request_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM fill_requests WHERE id = ?", (request_id,)
        ).fetchone()
    return _to_dict(row) if row else None


def for_posting(user_id: str, posting_id: int) -> dict | None:
    """The most recent request for a posting (any status), for the review page."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM fill_requests WHERE user_id = ? AND posting_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (user_id, posting_id),
        ).fetchone()
    return _to_dict(row) if row else None


def list_for_user(user_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM fill_requests WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
    return [_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------

def _advance(request_id: int, frm: str, to: str, **cols) -> bool:
    sets = "".join(f", {k} = :{k}" for k in cols)
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE fill_requests SET status = :to, updated_at = :now{sets} "
            "WHERE id = :id AND status = :frm",
            {"to": to, "now": _now(), "id": request_id, "frm": frm, **cols},
        )
        return cur.rowcount > 0


def _to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["preview"] = json.loads(d.pop("preview_json")) if d.get("preview_json") else None
    return d
