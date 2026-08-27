"""Per-user daily cap on paid Anthropic calls.

The global token bucket (``app.ratelimit``) still limits burst. This keeps one
beta tester from burning the shared key and starving everyone else's discovery
scoring. Callers fail open to heuristics/templates when the cap is hit.

Set the current user with ``for_user`` (chat, discovery) or ``set_user``
(request handlers). ``consume`` records one call if the cap allows it.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone

from .config import get_settings
from .db import connect

_user: ContextVar[str] = ContextVar("llm_budget_user", default="")


def set_user(user_id: str | None) -> None:
    _user.set((user_id or "").strip())


@contextmanager
def for_user(user_id: str | None):
    token = _user.set((user_id or "").strip())
    try:
        yield
    finally:
        _user.reset(token)


def current_user() -> str:
    return _user.get() or "_anon"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def calls_today(user_id: str | None = None) -> int:
    uid = (user_id or current_user()).strip() or "_anon"
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM llm_usage WHERE user_id = ? AND day = ?",
            (uid, _today()),
        ).fetchone()
    return int(row["n"] if row is not None else 0)


def consume(user_id: str | None = None) -> bool:
    """Reserve one paid call. False means the caller should skip the LLM."""
    s = get_settings()
    cap = int(s.llm_max_calls_per_user_per_day or 0)
    if cap <= 0:
        return True
    uid = (user_id or current_user()).strip() or "_anon"
    if uid:
        set_user(uid)
    if calls_today(uid) >= cap:
        return False
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connect() as conn:
        conn.execute(
            "INSERT INTO llm_usage (user_id, day, created_at) VALUES (?, ?, ?)",
            (uid, _today(), now),
        )
    return True
