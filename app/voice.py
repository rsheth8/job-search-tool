"""The small personal bits: the user's name, time of day, push titles.

Identity is for Autofill (legal first name on forms). This module picks the name
you'd actually say — preferred_name, then first_name, then the first token of the
Apple display name — and keeps it off the legal-name path.

Time of day for push uses the IANA timezone the phone sent with its device token.
The iOS UI uses the phone's clock directly; this is only for banners that fire
while the app is closed.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import applicant, auth
from .db import connect

HORIZON_BLURB = (
    "I'm Horizon. I can help you find jobs, edit your form details, "
    "and walk you through Autofill. You always tap Submit."
)

_MAX_NAME = 24
_EMAILISH = re.compile(r"@")
_FOLLOW_UP = re.compile(r"Follow up with (.+?)\??\s*$", re.I | re.M)


def first_name(user_id: str) -> str:
    """Conversational first name, or empty if we don't know one."""
    ident = applicant.get_identity(user_id) if user_id else {}
    user = auth.get_user(user_id) if user_id else None
    display = (user or {}).get("display_name") or ""
    return name_from(ident, display)


def name_from(identity: dict | None, display_name: str | None = None) -> str:
    """Resolve a first name from an identity dict and/or Apple display name."""
    ident = identity or {}
    for key in ("preferred_name", "first_name"):
        token = _token(ident.get(key))
        if token:
            return token
    return _token(display_name)


def _token(raw) -> str:
    text = str(raw or "").strip()
    if not text or _EMAILISH.search(text):
        return ""
    piece = text.split()[0].strip(".,!?")
    if not piece or len(piece) > _MAX_NAME:
        return ""
    return piece


def timezone_for(user_id: str) -> str | None:
    """Most recently updated device timezone, if the phone has registered one."""
    if not user_id:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT timezone FROM device_tokens "
            "WHERE user_id = ? AND timezone IS NOT NULL AND timezone != '' "
            "ORDER BY updated_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    tz = (row["timezone"] if row else None) or None
    return tz.strip() if isinstance(tz, str) and tz.strip() else None


def local_hour(user_id: str, *, now: datetime | None = None) -> int | None:
    tz_name = timezone_for(user_id)
    if not tz_name:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, Exception):  # noqa: BLE001 — bad IANA id
        return None
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(tz).hour


def daypart(hour: int | None) -> str:
    """morning / afternoon / evening, or empty late at night / unknown."""
    if hour is None:
        return ""
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return ""


def daypart_from_text(text: str) -> str:
    """Honor a time-of-day the user already said (good morning / afternoon / …)."""
    low = (text or "").lower()
    if "good morning" in low:
        return "morning"
    if "good afternoon" in low:
        return "afternoon"
    if "good evening" in low or "good night" in low:
        return "evening"
    return ""


def hello(user_id: str = "", *, hour: int | None = None, text: str = "",
          name: str | None = None) -> str:
    """'Good morning, Ada' / 'Hey Ada' / 'Hey'."""
    who = first_name(user_id) if name is None else name
    part = daypart_from_text(text) or daypart(hour)
    if part and who:
        return f"Good {part}, {who}"
    if part and not who:
        return f"Good {part}"
    if who:
        return f"Hey {who}"
    return "Hey"


def greeting_reply(user_id: str, text: str = "") -> str:
    hour = None if daypart_from_text(text) else local_hour(user_id)
    return f"{hello(user_id, hour=hour, text=text)} — {HORIZON_BLURB}"


def with_name(user_id: str, named: str, plain: str) -> str:
    """Pick the named variant when we have a first name, else the plain one.

    ``named`` may include ``{name}``.
    """
    who = first_name(user_id)
    if who:
        return named.format(name=who)
    return plain


def match_notification(user_id: str, count: int, top: str | None = None,
                       *, hour: int | None = None) -> tuple[str, str]:
    """Lock-screen title + body for new matches."""
    who = first_name(user_id)
    part = daypart(hour if hour is not None else local_hour(user_id))
    noun = "match" if count == 1 else "matches"
    lead = f"{count} new {noun}" if count != 1 else "A new match"
    detail = (top or "").strip()

    if who and part:
        title = f"Good {part}, {who}"
        body = f"{lead} — {detail}" if detail else f"{lead}. Open the app to review."
        return title, body
    if who:
        title = f"{who}, {count} new {noun}" if count != 1 else f"{who}, a new match"
        body = detail or "Open the app to review."
        return title, body
    title = f"{count} new {noun}"
    body = detail or "Open the app to review them."
    return title, body


def reminder_notification(user_id: str, body: str) -> tuple[str, str]:
    """Lock-screen title + body for a follow-up (or other chat ping)."""
    who = first_name(user_id)
    text = _strip_emoji((body or "").strip())
    company = _followup_company(body)
    if company:
        title = f"{who}, a follow-up" if who else "Follow up"
        preview = f"{company} — want to check in?"
        return title, preview
    title = f"Hey {who}" if who else "JobPilot"
    preview = text if len(text) <= 160 else text[:157] + "…"
    return title, preview


def _followup_company(body: str) -> str:
    match = _FOLLOW_UP.search(_strip_emoji(body or ""))
    if not match:
        return ""
    return match.group(1).strip().rstrip(".")


def _strip_emoji(text: str) -> str:
    return re.sub(r"^[^\w]+", "", text or "").strip()
