"""Reminder scheduling — the Phase 2 core, decoupled from delivery.

A reminder is just a row with a due time. A *sender* ships it when due:

  - ``LogSender`` (default now): writes the reminder to the log. Lets the whole
    pipeline run and be tested while outbound SMS is blocked on A2P 10DLC.
  - ``TwilioSender`` (later): one drop-in class that calls the Twilio REST API.
    ``deliver_due_reminders`` doesn't change — only which sender is passed in.

Everything here is import-light and synchronous so it's trivially testable. The
APScheduler loop in ``app/scheduler.py`` is a thin wrapper that calls
``deliver_due_reminders`` on a timer; the scheduler is the only place that
imports apscheduler, and nothing here depends on it.

Timezone note: times are computed and stored in UTC. A personal single-user
tool doesn't need per-message localization yet; revisit if reminders feel
"off by hours" in practice.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Protocol

from . import store
from .db import connect

logger = logging.getLogger("reminders")

_UNIT_DAYS = {
    "hour": 1 / 24,
    "hr": 1 / 24,
    "day": 1.0,
    "week": 7.0,
    "month": 30.0,
}
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time_reference(text: str | None, *, now: datetime | None = None) -> datetime | None:
    """Best-effort natural-time parse → absolute UTC datetime, or None.

    Handles the common SMS shapes: "today", "tonight", "tomorrow", "in 3 days",
    "in 2 weeks", "next week/month", and bare weekday names ("friday"). Returns
    None when it can't tell — the caller decides on a fallback (never hard-fail).
    """
    if not text:
        return None
    now = now or _now()
    t = text.strip().lower()

    if t in ("now", "asap"):
        return now
    if t == "today":
        return now + timedelta(hours=3)
    if t == "tonight":
        return now.replace(hour=20, minute=0, second=0, microsecond=0)
    if t == "tomorrow":
        return now + timedelta(days=1)
    if t in ("next week", "in a week"):
        return now + timedelta(days=7)
    if t in ("next month", "in a month"):
        return now + timedelta(days=30)

    # "in N unit(s)"
    m = re.search(r"in\s+(\d+)\s+(hour|hr|day|week|month)s?", t)
    if m:
        n = int(m.group(1))
        return now + timedelta(days=n * _UNIT_DAYS[m.group(2)])

    # bare "N day(s)" / "N weeks"
    m = re.search(r"(\d+)\s+(hour|hr|day|week|month)s?", t)
    if m:
        n = int(m.group(1))
        return now + timedelta(days=n * _UNIT_DAYS[m.group(2)])

    # weekday name → next occurrence (strictly in the future)
    for name, idx in _WEEKDAYS.items():
        if name in t:
            ahead = (idx - now.weekday()) % 7
            ahead = ahead or 7
            return (now + timedelta(days=ahead)).replace(
                hour=9, minute=0, second=0, microsecond=0
            )
    return None


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def create_reminder(
    user_id: str,
    remind_at: datetime,
    body: str,
    *,
    application_id: int | None = None,
) -> sqlite3.Row:
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO reminders
                (user_id, application_id, remind_at, body, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (user_id, application_id, remind_at.isoformat(), body, now.isoformat()),
        )
        return conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (cur.lastrowid,)
        ).fetchone()


def due_reminders(now: datetime | None = None) -> list[sqlite3.Row]:
    now = now or _now()
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM reminders WHERE status = 'pending' AND remind_at <= ? "
            "ORDER BY remind_at",
            (now.isoformat(),),
        ).fetchall()


def mark_sent(reminder_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE reminders SET status = 'sent', sent_at = ? WHERE id = ?",
            (_now().isoformat(), reminder_id),
        )


def list_pending(user_id: str) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM reminders WHERE user_id = ? AND status = 'pending' "
            "ORDER BY remind_at",
            (user_id,),
        ).fetchall()


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

class Sender(Protocol):
    def send(self, user_id: str, body: str) -> None: ...


class LogSender:
    """Fallback sender: records + logs instead of sending real SMS.

    Used until outbound Twilio is configured (A2P 10DLC). ``sent`` is kept so
    tests (and a curious operator) can see what would have gone out.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, user_id: str, body: str) -> None:
        self.sent.append((user_id, body))
        logger.info("[reminder→%s] %s", user_id, body)


class TwilioSender:
    """Live outbound SMS via the Twilio REST API.

    Dormant until ``TWILIO_ACCOUNT_SID`` + ``TWILIO_AUTH_TOKEN`` +
    ``TWILIO_FROM_NUMBER`` are set (gated on A2P 10DLC approval). The Client is
    built once and reused. ``user_id`` is the recipient's phone number — it's
    the inbound ``From`` we already store, so no mapping is needed.
    """

    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        from twilio.rest import Client  # lazy: only imported when actually sending

        self._client = Client(account_sid, auth_token)
        self._from = from_number

    def send(self, user_id: str, body: str) -> None:
        self._client.messages.create(to=user_id, from_=self._from, body=body)
        logger.info("[reminder→%s via twilio] %s", user_id, body)


class AppSender:
    """In-app delivery: append to the chat transcript + APNs push.

    Primary channel now that Slack is retired. Push is best-effort (no-op when
    APNs isn't configured); the transcript always lands so Chat shows it.
    ``sent`` mirrors LogSender so tests (and operators) can inspect deliveries.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, user_id: str, body: str) -> None:
        from . import chat, push

        text = (body or "").strip()
        if not text:
            return
        self.sent.append((user_id, text))
        try:
            chat.append(user_id, "assistant", text)
        except Exception:  # noqa: BLE001
            logger.exception("failed to append chat for %s", user_id)
        # Truncate for the banner; full text is in Chat.
        title = "Apply"
        preview = text if len(text) <= 160 else text[:157] + "…"
        try:
            push.send(user_id, title, preview, data={"kind": "chat"})
        except Exception:  # noqa: BLE001
            logger.exception("push failed for %s", user_id)
        logger.info("[reminder→%s via app] %s", user_id, preview)


_sender_singleton: Sender | None = None


def get_sender() -> Sender:
    """Pick the sender from config: App (chat+push), optional Slack, Twilio, else Log.

    In-app chat is the primary channel. Slack only if explicitly re-enabled.
    Cached so the client isn't rebuilt every poll tick. Tests pass an explicit
    sender, so they never touch this.
    """
    global _sender_singleton
    if _sender_singleton is not None:
        return _sender_singleton
    from .config import get_settings

    s = get_settings()
    # Prefer AppSender whenever we have any registered devices *or* simply as
    # the default product channel — transcript write is always useful, push is
    # best-effort. Only fall through when an operator explicitly wants Slack.
    if s.slack_enabled:
        from .slack import SlackSender

        _sender_singleton = SlackSender(s.slack_bot_token)
        logger.info("reminder delivery: Slack enabled (legacy)")
    elif s.outbound_sms_enabled:
        _sender_singleton = TwilioSender(
            s.twilio_account_sid, s.twilio_auth_token, s.twilio_from_number
        )
        logger.info("reminder delivery: Twilio outbound enabled")
    else:
        _sender_singleton = AppSender()
        logger.info("reminder delivery: AppSender (chat + push)")
    return _sender_singleton


def deliver_due_reminders(
    sender: Sender | None = None, *, now: datetime | None = None
) -> int:
    """Ship every due reminder via ``sender``; return how many went out.

    A send failure for one reminder is logged and the reminder stays pending
    (it'll retry next tick) — one bad row never blocks the rest.
    """
    sender = sender or get_sender()
    count = 0
    for r in due_reminders(now):
        try:
            sender.send(r["user_id"], r["body"])
        except Exception:  # noqa: BLE001 — never let one failure stop the batch
            logger.exception("reminder %s failed to send; leaving pending", r["id"])
            continue
        mark_sent(r["id"])
        count += 1
    return count


def build_body(company: str, *, note: str | None = None) -> str:
    base = f"⏰ Follow up with {company}?"
    return f"{base}\n{note}" if note else base


def schedule_for_company(
    user_id: str,
    company: str,
    time_reference: str | None,
    *,
    now: datetime | None = None,
    fallback_days: int,
) -> tuple[sqlite3.Row, datetime, bool]:
    """Create a reminder for a company, resolving the time reference.

    Returns (reminder_row, remind_at, parsed) where ``parsed`` is False when we
    had to fall back to the default window because the time couldn't be read.
    """
    now = now or _now()
    when = parse_time_reference(time_reference, now=now)
    parsed = when is not None
    if when is None:
        when = now + timedelta(days=fallback_days)
    app = store.find_application(user_id, company)
    row = create_reminder(
        user_id, when, build_body(company),
        application_id=app["id"] if app else None,
    )
    return row, when, parsed
