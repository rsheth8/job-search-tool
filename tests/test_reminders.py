"""Reminder scheduling + delivery tests (offline, no real scheduler)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import config, reminders, store
from app.engine import handle_sms


def _now() -> datetime:
    return datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)  # a Monday


# --- time parsing -----------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected_days",
    [
        ("tomorrow", 1),
        ("in 3 days", 3),
        ("in 2 weeks", 14),
        ("next week", 7),
        ("in 1 month", 30),
    ],
)
def test_parse_relative(text, expected_days):
    now = _now()
    when = reminders.parse_time_reference(text, now=now)
    assert when is not None
    assert round((when - now).total_seconds() / 86400) == expected_days


def test_parse_weekday_is_future():
    now = _now()  # Monday
    when = reminders.parse_time_reference("friday", now=now)
    assert when.weekday() == 4
    assert when > now


def test_parse_unknown_returns_none():
    assert reminders.parse_time_reference("whenever-ish", now=_now()) is None
    assert reminders.parse_time_reference(None) is None


# --- persistence + delivery -------------------------------------------------

def test_due_filtering_respects_remind_at():
    now = _now()
    reminders.create_reminder("u", now - timedelta(minutes=1), "past due")
    reminders.create_reminder("u", now + timedelta(days=1), "future")
    due = reminders.due_reminders(now)
    assert [r["body"] for r in due] == ["past due"]


def test_deliver_marks_sent_and_uses_sender():
    now = _now()
    reminders.create_reminder("u", now - timedelta(minutes=1), "ping")
    sender = reminders.LogSender()
    sent = reminders.deliver_due_reminders(sender, now=now)
    assert sent == 1
    assert sender.sent == [("u", "ping")]
    # Idempotent: a second pass delivers nothing (status flipped to 'sent').
    assert reminders.deliver_due_reminders(sender, now=now) == 0


def test_failed_send_leaves_reminder_pending():
    now = _now()
    reminders.create_reminder("u", now - timedelta(minutes=1), "ping")

    class Boom:
        def send(self, user_id, body):
            raise RuntimeError("network down")

    assert reminders.deliver_due_reminders(Boom(), now=now) == 0
    # Still pending → will retry next tick.
    assert len(reminders.list_pending("u")) == 1


def test_schedule_for_company_links_application():
    app = store.create_application("u", "Stripe", "SWE")
    row, when, parsed = reminders.schedule_for_company(
        "u", "Stripe", "in 2 days", now=_now(), fallback_days=7
    )
    assert parsed is True
    assert row["application_id"] == app["id"]
    assert round((datetime.fromisoformat(row["remind_at"]) - _now()).days) == 2


def test_schedule_falls_back_when_time_unparseable():
    _row, _when, parsed = reminders.schedule_for_company(
        "u", "Stripe", "sometime maybe", fallback_days=7
    )
    assert parsed is False


# --- engine wiring (REMIND intent) ------------------------------------------

# --- sender selection -------------------------------------------------------

def test_get_sender_defaults_to_app_sender():
    # conftest leaves Twilio/Slack unset → in-app chat + push.
    assert isinstance(reminders.get_sender(), reminders.AppSender)


def test_get_sender_picks_twilio_when_configured(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACfake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "faketoken")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+14155550100")
    config.get_settings.cache_clear()
    reminders._sender_singleton = None
    assert config.get_settings().outbound_sms_enabled is True
    assert isinstance(reminders.get_sender(), reminders.TwilioSender)


def test_partial_twilio_config_stays_on_app_sender(monkeypatch):
    # SID + token but no from-number → not enough to send.
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACfake")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "faketoken")
    config.get_settings.cache_clear()
    reminders._sender_singleton = None
    assert config.get_settings().outbound_sms_enabled is False
    assert isinstance(reminders.get_sender(), reminders.AppSender)


def test_remind_intent_creates_pending_reminder():
    handle_sms("remuser", "applied notion swe")
    reply = handle_sms("remuser", "remind me about notion in 3 days")
    assert "notion" in reply.lower()
    pending = reminders.list_pending("remuser")
    assert len(pending) == 1
    assert "Notion" in pending[0]["body"]
