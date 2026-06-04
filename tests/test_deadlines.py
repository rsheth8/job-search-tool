"""Dates & deadlines (offline)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import deadlines, reminders, store
from app.engine import handle_sms


def _now() -> datetime:
    return datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)  # a Monday


# --- core -------------------------------------------------------------------

def test_create_deadline_and_upcoming():
    when = _now() + timedelta(days=3)
    deadlines.create_deadline("u", "Stripe", "OA", when, schedule_reminder=False)
    items = deadlines.upcoming("u", now=_now())
    assert len(items) == 1
    assert items[0]["company"] == "Stripe"
    assert items[0]["label"] == "OA"


def test_upcoming_sorted_and_excludes_past():
    deadlines.create_deadline("u", "A", "OA", _now() + timedelta(days=5), schedule_reminder=False)
    deadlines.create_deadline("u", "B", "Onsite", _now() + timedelta(days=1), schedule_reminder=False)
    deadlines.create_deadline("u", "C", "Interview", _now() - timedelta(days=1), schedule_reminder=False)
    items = deadlines.upcoming("u", now=_now())
    assert [d["company"] for d in items] == ["B", "A"]  # past C excluded, sorted


def test_create_deadline_schedules_reminder():
    when = _now() + timedelta(days=3)
    deadlines.create_deadline("u", "Stripe", "OA", when)  # schedule_reminder defaults True
    pending = reminders.list_pending("u")
    assert len(pending) == 1
    assert "Stripe" in pending[0]["body"]
    # Heads-up fires a day before the deadline.
    remind_at = datetime.fromisoformat(pending[0]["remind_at"])
    assert (when - remind_at).days == 1


def test_label_from_status_and_keywords():
    assert deadlines.label_from("stripe oa due friday", "OA received") == "OA"
    assert deadlines.label_from("final round next week", None) == "Onsite"
    assert deadlines.label_from("submit the thing friday", None) == "Deadline"


# --- engine wiring ----------------------------------------------------------

def test_deadline_intent_sets_and_confirms():
    store.create_application("d", "Stripe", "SWE")
    reply = handle_sms("d", "stripe oa due friday")
    assert "Stripe" in reply
    assert "OA" in reply
    assert deadlines.upcoming("d")  # persisted


def test_agenda_query_lists_upcoming():
    handle_sms("d", "stripe oa due friday")
    reply = handle_sms("d", "what's coming up")
    assert "Stripe" in reply
    assert "Upcoming" in reply


def test_empty_agenda():
    reply = handle_sms("d", "what's coming up")
    assert "Nothing on the calendar" in reply


def test_update_with_date_adds_calendar_item():
    store.create_application("d", "Google", "SWE")
    reply = handle_sms("d", "google onsite next tuesday")
    assert "Onsite" in reply
    assert "calendar" in reply.lower()
    items = deadlines.upcoming("d")
    assert any(d["company"] == "Google" and d["label"] == "Onsite" for d in items)
