"""Tester feedback inbox (invite-only beta)."""
from __future__ import annotations

from datetime import datetime, timezone

from .config import get_settings
from .db import connect


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def add(user_id: str, body: str) -> dict:
    text = (body or "").strip()
    if not text:
        raise ValueError("empty feedback")
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO feedback (user_id, body, created_at) VALUES (?, ?, ?)",
            (user_id, text, now),
        )
        row = conn.execute(
            "SELECT * FROM feedback WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    _maybe_notify(user_id, text)
    return dict(row)


def list_recent(limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit or 50), 200))
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def _maybe_notify(user_id: str, body: str) -> None:
    dest = get_settings().feedback_notify_user.strip()
    if not dest:
        return
    try:
        from . import reminders

        snippet = body if len(body) <= 400 else body[:397] + "…"
        reminders.get_sender().send(
            dest, f"Beta feedback from {user_id}:\n{snippet}"
        )
    except Exception:  # noqa: BLE001 — storing the row is enough
        pass
