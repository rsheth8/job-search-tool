"""Conversational corrections to past entries (EDIT). All offline."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import store
from app.engine import handle_sms
from app.intents import Intent
from app.router import HeuristicRouter

R = HeuristicRouter()


# --- store layer ------------------------------------------------------------

def test_edit_application_changes_only_given_fields():
    app = store.create_application("u", "Stripe", "SWE")
    store.edit_application("u", app["id"], role="SWE II")
    row = store.find_application("u", "Stripe")
    assert row["role"] == "SWE II"
    assert row["company"] == "Stripe"  # untouched


def test_edit_application_rename_and_date():
    app = store.create_application("u", "Databricks", "SWE")
    when = datetime(2026, 3, 1, tzinfo=timezone.utc)
    store.edit_application("u", app["id"], company="Databricks Inc", applied_at=when)
    row = store.get_application("u", app["id"])
    assert row["company"] == "Databricks Inc"
    assert row["applied_at"].startswith("2026-03-01")
    # An 'edit' event is recorded for history.
    assert any(e["type"] == "edit" for e in store.list_events("u", app["id"]))


# --- routing ----------------------------------------------------------------

@pytest.mark.parametrize("text,role", [
    ("change the stripe role to SWE II", "SWE II"),
    ("stripe is actually a Backend Engineer role", "Backend Engineer"),
    ("fix the stripe role to Data Scientist", "Data Scientist"),
])
def test_edit_role_phrasings(text, role):
    p = R.parse(text)
    assert p.intent == Intent.EDIT, text
    assert p.company == "Stripe", text
    assert p.role == role, text


def test_rename_routes_to_edit_with_new_name_in_message():
    p = R.parse("rename databricks to Databricks Inc")
    assert p.intent == Intent.EDIT
    assert p.company == "Databricks"
    assert p.message == "Databricks Inc"


def test_change_to_status_is_update_not_edit():
    # "change X to <stage>" is a stage change, not an attribute correction.
    p = R.parse("change stripe to onsite")
    assert p.intent == Intent.UPDATE
    assert p.status == "Onsite"


# --- engine -----------------------------------------------------------------

def test_edit_role_end_to_end():
    store.create_application("u", "Stripe", "SWE")
    reply = handle_sms("u", "change the stripe role to SWE II")
    assert "SWE II" in reply
    assert store.find_application("u", "Stripe")["role"] == "SWE II"


def test_edit_rename_end_to_end():
    store.create_application("u", "Figma", "Designer")
    handle_sms("u", "rename figma to Figma Inc")
    assert store.find_application("u", "Figma Inc") is not None
    assert store.find_application("u", "Figma") is None


def test_edit_unknown_company():
    reply = handle_sms("u", "change the google role to SWE")
    assert "don't have Google" in reply


def test_edit_is_not_destructive_no_confirmation():
    store.create_application("u", "Stripe", "SWE")
    reply = handle_sms("u", "stripe is actually a PM role")
    assert "yes/no" not in reply.lower()  # corrections apply directly
    assert store.find_application("u", "Stripe")["role"] == "PM"
