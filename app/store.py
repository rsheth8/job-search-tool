"""Application + event persistence (the 'Application Engine' data side)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from .config import get_settings
from .db import connect


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# Closed stages need no follow-up nudge. Duplicated from scoring.TERMINAL_STATUSES
# (importing scoring here would be circular — scoring imports store).
_TERMINAL = {"Offer", "Rejected", "Ghosted"}


def _next_followup(from_dt: datetime, status: str | None) -> str | None:
    """When the next follow-up nudge is due, or None for a closed application."""
    if status in _TERMINAL:
        return None
    return _iso(from_dt + timedelta(days=get_settings().default_followup_days))


def create_application(
    user_id: str,
    company: str,
    role: str | None,
    *,
    status: str = "Applied",
    source: str = "sms",
    raw_sms: str | None = None,
    applied_at: datetime | None = None,
) -> sqlite3.Row:
    now = _now()
    # Backfilled apps can carry their real application date so staleness scoring
    # reflects true age; live SMS logging always uses "now".
    applied = applied_at or now
    followup = _next_followup(applied, status)
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO applications
                (user_id, company, role, status, applied_at, source,
                 next_follow_up_at, last_updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                company,
                role,
                status,
                _iso(applied),
                source,
                followup,
                _iso(applied),
            ),
        )
        app_id = cur.lastrowid
        conn.execute(
            """
            INSERT INTO application_events
                (application_id, type, content, timestamp, raw_sms)
            VALUES (?, 'created', ?, ?, ?)
            """,
            (app_id, f"Applied to {company}" + (f" — {role}" if role else ""),
             _iso(applied), raw_sms),
        )
        app = conn.execute(
            "SELECT * FROM applications WHERE id = ?", (app_id,)
        ).fetchone()
    from . import jobstore

    jobstore.mark_matching_postings_applied(user_id, company, role)
    return app


def find_application(
    user_id: str, company: str | None, *, role: str | None = None
) -> sqlite3.Row | None:
    """Most recent application matching company (and role if given), case-insensitive."""
    if not company:
        return None
    sql = (
        "SELECT * FROM applications WHERE user_id = ? "
        "AND lower(company) = lower(?) "
    )
    params: list = [user_id, company]
    if role:
        sql += "AND lower(coalesce(role,'')) = lower(?) "
        params.append(role)
    sql += "ORDER BY last_updated_at DESC LIMIT 1"
    with connect() as conn:
        return conn.execute(sql, params).fetchone()


def get_application(app_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM applications WHERE id = ?", (app_id,)
        ).fetchone()


def update_status(app_id: int, status: str, *, raw_sms: str | None = None) -> sqlite3.Row:
    now = _now()
    with connect() as conn:
        conn.execute(
            "UPDATE applications SET status = ?, last_updated_at = ?, "
            "next_follow_up_at = ? WHERE id = ?",
            (status, _iso(now), _next_followup(now, status), app_id),
        )
        conn.execute(
            """
            INSERT INTO application_events
                (application_id, type, content, timestamp, raw_sms)
            VALUES (?, 'status', ?, ?, ?)
            """,
            (app_id, status, _iso(now), raw_sms),
        )
        return conn.execute(
            "SELECT * FROM applications WHERE id = ?", (app_id,)
        ).fetchone()


def add_note(app_id: int, note: str, *, raw_sms: str | None = None) -> None:
    now = _now()
    with connect() as conn:
        row = conn.execute(
            "SELECT status FROM applications WHERE id = ?", (app_id,)
        ).fetchone()
        status = row["status"] if row else None
        conn.execute(
            "UPDATE applications SET last_updated_at = ?, next_follow_up_at = ? "
            "WHERE id = ?",
            (_iso(now), _next_followup(now, status), app_id),
        )
        conn.execute(
            """
            INSERT INTO application_events
                (application_id, type, content, timestamp, raw_sms)
            VALUES (?, 'note', ?, ?, ?)
            """,
            (app_id, note, _iso(now), raw_sms),
        )


def list_applications(user_id: str, *, limit: int = 25) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM applications WHERE user_id = ? "
            "ORDER BY last_updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def last_application(user_id: str) -> sqlite3.Row | None:
    rows = list_applications(user_id, limit=1)
    return rows[0] if rows else None


def edit_application(
    app_id: int,
    *,
    company: str | None = None,
    role: str | None = None,
    applied_at: datetime | None = None,
    raw_sms: str | None = None,
) -> sqlite3.Row:
    """Correct stored attributes of an application (not a stage change).

    Only the provided fields change. Records an 'edit' event describing the
    correction so the history stays honest.
    """
    now = _now()
    changes: list[str] = []
    sets: list[str] = []
    params: list = []
    if company is not None:
        sets.append("company = ?")
        params.append(company)
        changes.append(f"company → {company}")
    if role is not None:
        sets.append("role = ?")
        params.append(role)
        changes.append(f"role → {role}")
    if applied_at is not None:
        sets.append("applied_at = ?")
        params.append(_iso(applied_at))
        changes.append(f"applied date → {applied_at.date().isoformat()}")
    sets.append("last_updated_at = ?")
    params.append(_iso(now))
    with connect() as conn:
        conn.execute(
            f"UPDATE applications SET {', '.join(sets)} WHERE id = ?",
            (*params, app_id),
        )
        conn.execute(
            """
            INSERT INTO application_events
                (application_id, type, content, timestamp, raw_sms)
            VALUES (?, 'edit', ?, ?, ?)
            """,
            (app_id, "; ".join(changes) or "edited", _iso(now), raw_sms),
        )
        return conn.execute(
            "SELECT * FROM applications WHERE id = ?", (app_id,)
        ).fetchone()


def delete_application(app_id: int) -> None:
    """Remove an application. Events cascade-delete; reminders/deadlines/recruiter
    rows keep their data but null out the link (ON DELETE SET NULL)."""
    with connect() as conn:
        conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))


def list_events(app_id: int, *, limit: int = 100) -> list[sqlite3.Row]:
    """Event timeline for one application, oldest first."""
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM application_events WHERE application_id = ? "
            "ORDER BY timestamp, id LIMIT ?",
            (app_id, limit),
        ).fetchall()


def applications_in_window(
    user_id: str, start: datetime, end: datetime | None = None, *, limit: int = 200
) -> list[sqlite3.Row]:
    """Applications whose ``applied_at`` falls in [start, end).

    Drives relative-date queries ("what did I apply to this week"). Ordered
    newest-first like ``list_applications``.
    """
    sql = (
        "SELECT * FROM applications WHERE user_id = ? AND applied_at >= ? "
    )
    params: list = [user_id, _iso(start)]
    if end is not None:
        sql += "AND applied_at < ? "
        params.append(_iso(end))
    sql += "ORDER BY applied_at DESC LIMIT ?"
    params.append(limit)
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


# --- undo support -----------------------------------------------------------

def last_event_id(app_id: int, event_type: str) -> int | None:
    """Id of the most recent event of a type for an app (the one just written)."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM application_events WHERE application_id = ? AND type = ? "
            "ORDER BY id DESC LIMIT 1",
            (app_id, event_type),
        ).fetchone()
        return row["id"] if row else None


def delete_event(event_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM application_events WHERE id = ?", (event_id,))


# Columns undo is allowed to write back (guards the f-string SQL below).
_RESTORABLE = {"status", "company", "role", "applied_at", "last_updated_at"}


def restore_application(app_id: int, fields: dict) -> None:
    """Write prior field values straight back (for undo). No event is recorded —
    undo also deletes the original event, so the timeline reads as if nothing
    happened. Values are applied verbatim, so a column can be restored to NULL.
    """
    cols = [c for c in fields if c in _RESTORABLE]
    if not cols:
        return
    sets = ", ".join(f"{c} = ?" for c in cols)
    params = [fields[c] for c in cols]
    with connect() as conn:
        conn.execute(
            f"UPDATE applications SET {sets} WHERE id = ?",
            (*params, app_id),
        )


def record_undo(user_id: str, kind: str, payload: dict, summary: str) -> None:
    """Remember the most recent reversible action (overwrites any prior one)."""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO undo_log (user_id, kind, payload, summary, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                kind = excluded.kind, payload = excluded.payload,
                summary = excluded.summary, created_at = excluded.created_at
            """,
            (user_id, kind, json.dumps(payload), summary, _iso(_now())),
        )


def get_undo(user_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT kind, payload, summary FROM undo_log WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "kind": row["kind"],
        "payload": json.loads(row["payload"] or "{}"),
        "summary": row["summary"],
    }


def clear_undo(user_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM undo_log WHERE user_id = ?", (user_id,))


def has_recruiter_signal(app_id: int) -> bool:
    """True if we have a recruiter signal for this application's company.

    Two sources, either counts:
      * a discovered recruiter row for the same user+company (Phase 3, Apollo); or
      * a note mentioning "recruiter" (the original lightweight signal — still
        useful when discovery hasn't run).

    The follow-up scorer reads through this one helper, so the Apollo table slots
    in here without any scorer change.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM recruiters r "
            "JOIN applications a ON a.id = ? "
            "WHERE r.user_id = a.user_id AND lower(r.company) = lower(a.company) "
            "LIMIT 1",
            (app_id,),
        ).fetchone()
        if row is not None:
            return True
        row = conn.execute(
            "SELECT 1 FROM application_events "
            "WHERE application_id = ? AND type = 'note' "
            "AND lower(coalesce(content,'')) LIKE '%recruiter%' "
            "LIMIT 1",
            (app_id,),
        ).fetchone()
        return row is not None
