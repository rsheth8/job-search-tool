"""In-app chat transcript + send path.

The conversational brain stays ``engine.handle_sms``; this module is the
durable message log and the thin API-facing send helper. Outbound reminders /
digests can also append here so the Chat tab doubles as an inbox.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .db import connect
from .engine import handle_sms


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def append(user_id: str, role: str, body: str) -> dict:
    """Store one message. ``role`` is ``user`` or ``assistant``."""
    if role not in ("user", "assistant"):
        raise ValueError(f"bad role: {role}")
    text = (body or "").strip()
    if not text:
        raise ValueError("empty body")
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO chat_messages (user_id, role, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, role, text, now),
        )
        row = conn.execute(
            "SELECT * FROM chat_messages WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


def history(user_id: str, *, limit: int = 100, before_id: int | None = None) -> list[dict]:
    """Recent messages, oldest-first (chat UI order)."""
    limit = max(1, min(int(limit or 100), 500))
    with connect() as conn:
        if before_id:
            rows = conn.execute(
                "SELECT * FROM chat_messages WHERE user_id = ? AND id < ? "
                "ORDER BY id DESC LIMIT ?",
                (user_id, before_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM chat_messages WHERE user_id = ? "
                "ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
    return [dict(r) for r in reversed(rows)]


def send(user_id: str, text: str) -> dict:
    """Record the user turn, run the engine, record the reply. Returns both."""
    user_msg = append(user_id, "user", text)
    reply = handle_sms(user_id, text) or ""
    assistant_msg = append(user_id, "assistant", reply) if reply.strip() else None
    return {
        "reply": reply,
        "user_message": user_msg,
        "assistant_message": assistant_msg,
    }


def clear(user_id: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM chat_messages WHERE user_id = ?", (user_id,)
        )
        return cur.rowcount
