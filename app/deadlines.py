"""Concrete dated events — OA due Friday, interview next Tuesday, onsite.

Distinct from reminders: a reminder *fires* at a time; a deadline is a calendar
item you want to *see coming* (the upcoming view). Setting a deadline also
schedules a day-ahead reminder through the existing reminders pipeline, so the
heads-up rides the same sender (LogSender now, Twilio once A2P clears) with no
scheduler changes.

Time parsing is delegated to ``reminders.parse_time_reference`` so "friday",
"in 3 days", "next week" etc. behave identically everywhere.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from . import reminders, store
from .db import connect


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Map a status/keyword to a short calendar label.
_LABELS = {
    "OA received": "OA",
    "Phone screen": "Phone screen",
    "Interview": "Interview",
    "Onsite": "Onsite",
}
_KEYWORD_LABELS = [
    ("oa", "OA"),
    ("online assessment", "OA"),
    ("assessment", "OA"),
    ("onsite", "Onsite"),
    ("on-site", "Onsite"),
    ("final round", "Onsite"),
    ("phone screen", "Phone screen"),
    ("phone", "Phone screen"),
    ("call", "Phone screen"),
    ("interview", "Interview"),
    ("offer", "Offer decision"),
    ("application", "Application"),
    ("apply", "Application"),
]


def label_from(text: str | None, status: str | None) -> str:
    if status and status in _LABELS:
        return _LABELS[status]
    low = (text or "").lower()
    for kw, label in _KEYWORD_LABELS:
        if kw in low:
            return label
    return "Deadline"


def create_deadline(
    user_id: str,
    company: str,
    label: str,
    due_at: datetime,
    *,
    application_id: int | None = None,
    schedule_reminder: bool = True,
    now: datetime | None = None,
) -> sqlite3.Row:
    now = now or _now()
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO deadlines
                (user_id, application_id, company, label, due_at, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'open', ?)
            """,
            (user_id, application_id, company, label, due_at.isoformat(), now.isoformat()),
        )
        row = conn.execute(
            "SELECT * FROM deadlines WHERE id = ?", (cur.lastrowid,)
        ).fetchone()

    if schedule_reminder:
        # Heads-up a day before (or now, if the deadline is sooner than that).
        remind_at = max(now, due_at - timedelta(days=1))
        body = f"📅 {company} {label} due {_humanize(due_at, now)}"
        reminders.create_reminder(
            user_id, remind_at, body, application_id=application_id
        )
    return row


def upcoming(
    user_id: str, *, within_days: int | None = 21, now: datetime | None = None
) -> list[sqlite3.Row]:
    """Open deadlines from now forward, soonest first. ``within_days=None`` = all."""
    now = now or _now()
    sql = (
        "SELECT * FROM deadlines WHERE user_id = ? AND status = 'open' "
        "AND due_at >= ? "
    )
    params: list = [user_id, now.isoformat()]
    if within_days is not None:
        sql += "AND due_at <= ? "
        params.append((now + timedelta(days=within_days)).isoformat())
    sql += "ORDER BY due_at"
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def mark_done(deadline_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE deadlines SET status = 'done' WHERE id = ?", (deadline_id,)
        )


def _humanize(due: datetime, now: datetime) -> str:
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    days = (due.date() - now.date()).days
    if days < 0:
        return f"{-days}d ago"
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    if days < 7:
        return due.strftime("%A")  # weekday name
    return due.date().isoformat()


def render_upcoming(user_id: str, *, now: datetime | None = None) -> str:
    now = now or _now()
    items = upcoming(user_id, now=now)
    if not items:
        return "Nothing on the calendar in the next 3 weeks. 🎈"
    lines = ["📅 Upcoming:"]
    for d in items:
        due = datetime.fromisoformat(d["due_at"])
        lines.append(f"• {d['company']} — {d['label']} ({_humanize(due, now)})")
    return "\n".join(lines)
