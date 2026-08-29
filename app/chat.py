"""In-app chat transcript + send path.

The conversational brain stays ``engine.handle_sms`` / ``handle_action``; this
module is the durable message log and the thin API-facing send helper. Outbound
reminders / digests can also append here so the Chat tab doubles as an inbox.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .db import connect
from .engine import handle_action, handle_sms
from .intents import Intent, ParsedMessage

logger = logging.getLogger("chat")


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


def _envelope(user_id: str, reply: str, user_msg: dict, assistant_msg: dict | None,
              parsed) -> dict:
    from . import agent

    meta = agent.decorate(user_id, reply, parsed)
    return {
        "reply": reply,
        "user_message": user_msg,
        "assistant_message": assistant_msg,
        **meta,
    }


_EMPTY_REPLY = (
    "I didn't fully catch that. Try “show new jobs”, "
    "“how do I autofill?”, or “change my phone to …”."
)
_FAIL_REPLY = (
    "Something went wrong on our side. Try again in a moment — "
    "if it keeps happening, send feedback from Settings."
)


def complete(user_id: str, text: str, *, parsed=None) -> dict:
    """Record the turn, run the engine, decorate the reply for the Assistant UI."""
    from .router import get_router

    user_msg = append(user_id, "user", text)
    used = parsed
    try:
        if parsed is not None:
            reply = handle_action(user_id, parsed, text) or ""
        else:
            reply = handle_sms(user_id, text) or ""
            used = get_router().parse(text)
        reply = (reply or "").strip() or _EMPTY_REPLY
    except Exception:  # noqa: BLE001 — testers still get a turn, not a 500
        logger.exception("chat complete failed for %s", user_id)
        reply = _FAIL_REPLY
        if used is None:
            used = ParsedMessage(intent=Intent.UNKNOWN, confidence=0.0)
    assistant_msg = append(user_id, "assistant", reply)
    return _envelope(user_id, reply, user_msg, assistant_msg, used)


def send(user_id: str, text: str) -> dict:
    """Record the user turn, run the heuristic engine, record the reply."""
    return complete(user_id, text)


def send_action(user_id: str, action: str, slots: dict | None, raw_text: str) -> dict:
    """Structured agent turn from on-device classification.

    Low-confidence / UNKNOWN falls back to heuristic parse of ``raw_text``.
    """
    from . import agent

    text = (raw_text or "").strip()
    if not text:
        raise ValueError("empty body")
    parsed = agent.parsed_from_action(action, slots, text)
    if parsed.intent == Intent.UNKNOWN or parsed.confidence < 0.4:
        return complete(user_id, text)
    return complete(user_id, text, parsed=parsed)


def clear(user_id: str) -> int:
    """Wipe the transcript and any half-finished command for this user."""
    from . import conversation as convo

    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM chat_messages WHERE user_id = ?", (user_id,)
        )
        n = cur.rowcount
    convo.clear_pending(user_id)
    return n
