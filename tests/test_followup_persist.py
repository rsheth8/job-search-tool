"""next_follow_up_at is kept live as activity changes. Offline."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import store
from app.engine import handle_sms


def _parse(iso):
    return datetime.fromisoformat(iso)


def test_create_sets_followup_from_applied_date():
    app = store.create_application("u", "Stripe", "SWE")
    assert app["next_follow_up_at"] is not None
    delta = _parse(app["next_follow_up_at"]) - _parse(app["applied_at"])
    assert abs(delta - timedelta(days=7)) < timedelta(seconds=5)


def test_status_change_recomputes_followup():
    app = store.create_application(
        "u", "Stripe", "SWE", applied_at=datetime.now(timezone.utc) - timedelta(days=30)
    )
    old = app["next_follow_up_at"]
    store.update_status("u", app["id"], "Phone screen")
    fresh = store.get_application("u", app["id"])
    assert fresh["next_follow_up_at"] != old
    # Recomputed off "now", so it should be ~7 days out.
    assert _parse(fresh["next_follow_up_at"]) > datetime.now(timezone.utc) + timedelta(days=6)


def test_terminal_status_clears_followup():
    app = store.create_application("u", "Stripe", "SWE")
    store.update_status("u", app["id"], "Rejected")
    assert store.get_application("u", app["id"])["next_follow_up_at"] is None


def test_note_resets_the_followup_clock():
    app = store.create_application(
        "u", "Stripe", "SWE", applied_at=datetime.now(timezone.utc) - timedelta(days=30)
    )
    store.add_note("u", app["id"], "left a voicemail")
    fresh = store.get_application("u", app["id"])
    assert _parse(fresh["next_follow_up_at"]) > datetime.now(timezone.utc) + timedelta(days=6)


def test_check_shows_followup_for_open_app():
    handle_sms("u", "applied stripe swe")
    reply = handle_sms("u", "what's the status of stripe")
    assert "Follow-up due" in reply


def test_check_hides_followup_for_closed_app():
    handle_sms("u", "applied stripe swe")
    handle_sms("u", "stripe rejected")
    reply = handle_sms("u", "what's the status of stripe")
    assert "Follow-up due" not in reply
