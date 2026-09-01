"""Sitting + ranking progress — encouragement that does not reward spraying.

A *sitting* is three fitted files in the person's local day. Hitting the goal
is permission to stop, not a streak to protect. Ranking is the real incentive:
the reranker stays cold until they file *and* pass, so Pass is worth as much
as File.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import reranker, store, voice
from .config import get_settings

SITTING_GOAL = 3


def snapshot(user_id: str, *, now: datetime | None = None) -> dict:
    """JSON-safe progress for Apply and for 'how am I doing'."""
    now = now or datetime.now(timezone.utc)
    start, end = _day_bounds(user_id, now)
    filed_today = len(store.applications_in_window(user_id, start, end))
    goal = SITTING_GOAL
    sitting_done = filed_today >= goal
    likes, passes = reranker.class_counts(user_id)
    settings = get_settings()
    likes_need = settings.reranker_min_positive
    passes_need = settings.reranker_min_negative
    likes_left = max(0, likes_need - likes)
    passes_left = max(0, passes_need - passes)
    ranker_on = likes_left == 0 and passes_left == 0
    sitting_line = _sitting_line(filed_today, goal, sitting_done)
    ranker_line = None if ranker_on else _ranker_line(likes_left, passes_left)
    toast = sitting_line if sitting_done else f"{filed_today} of {goal} tonight."
    return {
        "filed_today": filed_today,
        "sitting_goal": goal,
        "sitting_done": sitting_done,
        "likes": likes,
        "passes": passes,
        "likes_need": likes_need,
        "passes_need": passes_need,
        "likes_left": likes_left,
        "passes_left": passes_left,
        "ranker_on": ranker_on,
        "sitting_line": sitting_line,
        "ranker_line": ranker_line,
        "toast": toast,
    }


def with_stats(user_id: str, body: str) -> str:
    """Append sitting/ranker lines to the pipeline stats reply."""
    snap = snapshot(user_id)
    parts = [body, snap["sitting_line"]]
    if snap["ranker_line"]:
        parts.append(snap["ranker_line"])
    return "\n".join(parts)


def _day_bounds(user_id: str, now: datetime) -> tuple[datetime, datetime]:
    tz_name = voice.timezone_for(user_id)
    try:
        tz = ZoneInfo(tz_name) if tz_name else timezone.utc
    except (ZoneInfoNotFoundError, Exception):  # noqa: BLE001 — bad IANA id
        tz = timezone.utc
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    local = now.astimezone(tz)
    start_local = datetime(local.year, local.month, local.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _sitting_line(filed: int, goal: int, done: bool) -> str:
    if filed == 0:
        return f"{_num(goal)} fitted files is a sitting — then stop."
    if not done:
        return f"{filed} of {goal} tonight."
    if filed == goal:
        return "Sitting done. That's enough for tonight."
    return f"{filed} filed today. Chase what's already out — don't spray."


def _ranker_line(likes_left: int, passes_left: int) -> str:
    bits = []
    if likes_left:
        bits.append(f"{likes_left} more file{'s' if likes_left != 1 else ''}")
    if passes_left:
        bits.append(f"{passes_left} more pass{'es' if passes_left != 1 else ''}")
    if not bits:
        return "Matches are ranked from what you file and pass."
    return "Ranking learns you after " + " and ".join(bits) + "."


def _num(n: int) -> str:
    words = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five"}
    return words.get(n, str(n))
