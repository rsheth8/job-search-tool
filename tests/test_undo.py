"""Single-level undo of the last reversible action. All offline."""
from __future__ import annotations

import pytest

from app import store
from app.engine import handle_sms
from app.intents import Intent
from app.router import HeuristicRouter

R = HeuristicRouter()


# --- routing ----------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "undo",
    "undo that",
    "revert",
    "revert my last change",
    "roll back",
    "take that back",
    "put it back",
])
def test_undo_phrases_route_to_undo(text):
    assert R.parse(text).intent is Intent.UNDO


# --- behavior ---------------------------------------------------------------

def test_undo_apply_removes_the_application():
    handle_sms("u", "applied stripe swe")
    reply = handle_sms("u", "undo")
    assert "↩️" in reply or "Undone" in reply
    assert store.find_application("u", "Stripe") is None


def test_undo_status_reverts_stage_and_drops_event():
    handle_sms("u", "applied stripe swe")
    handle_sms("u", "stripe rejected")
    app = store.find_application("u", "Stripe")
    assert app["status"] == "Rejected"
    handle_sms("u", "undo")
    app = store.find_application("u", "Stripe")
    assert app["status"] == "Applied"
    # The reverted status event is gone — history stays honest.
    assert not any(e["type"] == "status" for e in store.list_events(app["id"]))


def test_undo_note_removes_the_note():
    handle_sms("u", "applied stripe swe")
    handle_sms("u", "note stripe recruiter was great")
    app = store.find_application("u", "Stripe")
    assert any(e["type"] == "note" for e in store.list_events(app["id"]))
    handle_sms("u", "undo")
    assert not any(e["type"] == "note" for e in store.list_events(app["id"]))


def test_undo_edit_restores_prior_fields():
    handle_sms("u", "applied stripe swe")
    handle_sms("u", "change the stripe role to SWE II")
    assert store.find_application("u", "Stripe")["role"] == "SWE II"
    handle_sms("u", "undo")
    assert store.find_application("u", "Stripe")["role"] == "SWE"


def test_undo_bulk_restores_every_touched_app():
    handle_sms("u", "applied stripe swe")
    handle_sms("u", "applied ramp pm")
    handle_sms("u", "reject everything still in applied")
    handle_sms("u", "yes")
    assert store.find_application("u", "Stripe")["status"] == "Rejected"
    assert store.find_application("u", "Ramp")["status"] == "Rejected"
    handle_sms("u", "undo")
    assert store.find_application("u", "Stripe")["status"] == "Applied"
    assert store.find_application("u", "Ramp")["status"] == "Applied"


def test_undo_delete_is_a_tombstone_not_reversed():
    handle_sms("u", "applied stripe swe")
    handle_sms("u", "delete stripe")
    handle_sms("u", "yes")
    reply = handle_sms("u", "undo")
    assert "can't undo a delete" in reply.lower()
    # And it does not silently undo the action before the delete.
    assert store.find_application("u", "Stripe") is None


def test_undo_with_nothing_to_undo():
    reply = handle_sms("u", "undo")
    assert "nothing to undo" in reply.lower()


def test_undo_is_single_level_only():
    handle_sms("u", "applied stripe swe")
    handle_sms("u", "applied ramp pm")  # newest reversible action
    handle_sms("u", "undo")             # removes Ramp
    assert store.find_application("u", "Ramp") is None
    # A second undo has nothing left (single-level).
    reply = handle_sms("u", "undo")
    assert "nothing to undo" in reply.lower()
    assert store.find_application("u", "Stripe") is not None
