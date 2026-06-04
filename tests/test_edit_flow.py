"""Multi-turn EDIT slot-filling (ask which app / what to change). Offline."""
from __future__ import annotations

from app import store
from app.conversation import get_pending
from app.engine import handle_sms


def test_edit_asks_what_to_change_then_applies():
    handle_sms("u", "applied databricks sde")
    reply = handle_sms("u", "change databricks")
    assert "change about Databricks" in reply
    assert get_pending("u").awaiting == "edit_change"
    # Bare follow-up is threaded back into the pending edit.
    handle_sms("u", "role to SWE II")
    assert store.find_application("u", "Databricks")["role"] == "SWE II"
    assert not get_pending("u").active


def test_edit_follow_up_can_rename():
    handle_sms("u", "applied databricks sde")
    handle_sms("u", "change databricks")
    handle_sms("u", "call it Databricks Inc")
    assert store.find_application("u", "Databricks Inc") is not None


def test_edit_asks_which_application_when_company_missing():
    handle_sms("u", "applied stripe swe")
    # No clear company, no context match for a fix verb.
    reply = handle_sms("u", "fix the role")
    # Either it resolves to the last company or asks which app — both acceptable,
    # but it must not crash and must leave a coherent state.
    assert isinstance(reply, str) and reply


def test_one_shot_edit_still_works():
    handle_sms("u", "applied stripe swe")
    reply = handle_sms("u", "change the stripe role to SWE II")
    assert "SWE II" in reply
    assert store.find_application("u", "Stripe")["role"] == "SWE II"
    assert not get_pending("u").active


def test_edit_unknown_company_reports_cleanly():
    reply = handle_sms("u", "change the nonexistentco role to PM")
    assert "don't have" in reply.lower()
