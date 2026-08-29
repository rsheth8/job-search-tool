"""Conversational name, time-of-day greetings, and push copy."""
from __future__ import annotations

from datetime import datetime, timezone

from app import applicant, job_alerts, push, voice
from app.engine import handle_sms
from app.jobsources import JobPosting


def test_name_prefers_preferred_then_first_then_display():
    assert voice.name_from({"preferred_name": "Ace", "first_name": "Ada"}) == "Ace"
    assert voice.name_from({"first_name": "Ada Lovelace"}) == "Ada"
    assert voice.name_from({}, "Rahil Sheth") == "Rahil"
    assert voice.name_from({}, "ada@x.com") == ""
    assert voice.name_from({}) == ""


def test_first_name_reads_identity():
    applicant.set_identity("u1", {"preferred_name": "Ace", "first_name": "Ada"})
    assert voice.first_name("u1") == "Ace"
    applicant.set_identity("u1", {"preferred_name": ""})
    assert voice.first_name("u1") == "Ada"


def test_daypart_windows():
    assert voice.daypart(7) == "morning"
    assert voice.daypart(12) == "afternoon"
    assert voice.daypart(18) == "evening"
    assert voice.daypart(2) == ""
    assert voice.daypart(None) == ""


def test_hello_uses_said_time_of_day_and_name():
    assert voice.hello(name="Ada", text="good morning") == "Good morning, Ada"
    assert voice.hello(name="Ada", hour=15) == "Good afternoon, Ada"
    assert voice.hello(name="Ada", hour=2) == "Hey Ada"
    assert voice.hello(name="", hour=9) == "Good morning"
    assert voice.hello() == "Hey"


def test_greeting_reply_includes_horizon_and_name():
    applicant.set_identity("u1", {"first_name": "Ada"})
    reply = voice.greeting_reply("u1", "good morning")
    assert reply.startswith("Good morning, Ada")
    assert "Horizon" in reply
    assert "help" in reply.lower()


def test_handle_sms_greeting_uses_name():
    applicant.set_identity("sms-u", {"first_name": "Ada"})
    reply = handle_sms("sms-u", "hey")
    assert "Ada" in reply
    assert "Horizon" in reply


def test_signoff_uses_name():
    applicant.set_identity("sms-u", {"first_name": "Ada"})
    reply = handle_sms("sms-u", "bye")
    assert "Catch you later, Ada" in reply


def test_match_notification_with_name_and_hour():
    applicant.set_identity("u1", {"first_name": "Ada"})
    title, body = voice.match_notification(
        "u1", 3, "SWE @ Stripe", hour=9)
    assert title == "Good morning, Ada"
    assert "3 new matches" in body
    assert "SWE @ Stripe" in body


def test_match_notification_name_without_hour():
    applicant.set_identity("u1", {"first_name": "Ada"})
    title, body = voice.match_notification("u1", 1, "SWE @ Stripe", hour=3)
    assert title == "Ada, a new match"
    assert "SWE @ Stripe" in body


def test_match_notification_anonymous():
    title, body = voice.match_notification("nobody", 2, None, hour=9)
    assert title == "2 new matches"
    assert "review" in body.lower()


def test_reminder_notification_strips_emoji_and_names():
    applicant.set_identity("u1", {"first_name": "Ada"})
    title, body = voice.reminder_notification("u1", "⏰ Follow up with Stripe?")
    assert title == "Ada, a follow-up"
    assert "Stripe" in body
    assert "⏰" not in body


def test_timezone_from_device_token():
    push.register_device("u1", "tok-a", timezone="America/Chicago")
    assert voice.timezone_for("u1") == "America/Chicago"
    hour = voice.local_hour(
        "u1", now=datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc))
    # 14:00 UTC in June is 9:00 CDT
    assert hour == 9


def test_register_without_timezone_keeps_existing():
    push.register_device("u1", "tok-a", timezone="America/Chicago")
    push.register_device("u1", "tok-a")
    assert voice.timezone_for("u1") == "America/Chicago"


def test_digest_leads_with_name():
    posts = [
        (JobPosting("greenhouse", "1", "SWE", "https://x/1", company="Stripe"), 0.9, 10),
    ]
    applicant.set_identity("u1", {"first_name": "Ada"})
    body = job_alerts.build_digest(posts, user_id="u1")
    assert body.startswith("Ada — One new match")


def test_notify_new_matches_uses_personal_copy(monkeypatch):
    applicant.set_identity("u1", {"first_name": "Ada"})
    captured = []
    monkeypatch.setattr(
        push, "send",
        lambda uid, title, body, **k: captured.append((title, body)) or 1)
    assert push.notify_new_matches("u1", 2, "PM @ Ramp") == 1
    title, body = captured[0]
    assert "Ada" in title
    assert "PM @ Ramp" in body
