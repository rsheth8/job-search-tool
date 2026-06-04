"""Per-user conversational context memory.

Lets incomplete messages ("applied", "spotify update") resolve against the
last thing the user was talking about.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .db import connect


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_context(user_id: str) -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT last_company, last_role, last_application_id "
            "FROM context_memory WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return {"last_company": None, "last_role": None, "last_application_id": None}
    return dict(row)


def set_context(
    user_id: str,
    *,
    company: str | None = None,
    role: str | None = None,
    application_id: int | None = None,
) -> None:
    """Upsert context, only overwriting fields that are provided (non-None)."""
    current = get_context(user_id)
    company = company if company is not None else current["last_company"]
    role = role if role is not None else current["last_role"]
    application_id = (
        application_id if application_id is not None else current["last_application_id"]
    )
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO context_memory
                (user_id, last_company, last_role, last_application_id, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                last_company = excluded.last_company,
                last_role = excluded.last_role,
                last_application_id = excluded.last_application_id,
                updated_at = excluded.updated_at
            """,
            (user_id, company, role, application_id, _now()),
        )
