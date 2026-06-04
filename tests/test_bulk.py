"""Bulk/relative updates with mandatory two-step confirmation. All offline."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import store
from app.db import connect
from app.engine import handle_sms
from app.intents import Intent
from app.router import HeuristicRouter

R = HeuristicRouter()


def _backdate(app_id: int, days: int):
    old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with connect() as conn:
        conn.execute(
            "UPDATE applications SET last_updated_at = ? WHERE id = ?", (old, app_id)
        )


# --- routing ----------------------------------------------------------------

def test_bulk_parse_filter_and_target():
    p = R.parse("reject everything still in applied")
    assert p.intent == Intent.BULK
    assert p.status == "Rejected"
    assert p.message == "Applied"  # filter stage


def test_bulk_parse_mark_as():
    p = R.parse("mark all applied as ghosted")
    assert p.intent == Intent.BULK
    assert p.status == "Ghosted"
    assert p.message == "Applied"


def test_bulk_parse_age_filter():
    p = R.parse("ghost anything older than 30 days")
    assert p.intent == Intent.BULK
    assert p.status == "Ghosted"
    assert "30" in (p.time_reference or "")


def test_list_all_is_not_bulk():
    # "list all applications" has a quantifier but no action verb → stays LIST.
    assert R.parse("list all my applications").intent == Intent.LIST


# --- two-step confirmation (destructive) ------------------------------------

def test_bulk_requires_confirmation_then_applies():
    store.create_application("u", "A", None)
    store.create_application("u", "B", None)
    prompt = handle_sms("u", "reject everything still in applied")
    assert "2" in prompt and "yes" in prompt.lower()
    # Nothing changed yet — this is the safety step.
    assert all(a["status"] == "Applied" for a in store.list_applications("u"))
    done = handle_sms("u", "yes")
    assert "2" in done
    assert all(a["status"] == "Rejected" for a in store.list_applications("u"))


def test_bulk_cancel_changes_nothing():
    store.create_application("u", "A", None)
    handle_sms("u", "reject everything still in applied")
    handle_sms("u", "no")
    assert store.find_application("u", "A")["status"] == "Applied"


def test_bulk_preview_lists_companies():
    store.create_application("u", "Stripe", None)
    reply = handle_sms("u", "reject all applied")
    assert "Stripe" in reply
    assert "can't be undone" in reply.lower()


# --- filtering semantics ----------------------------------------------------

def test_bulk_filter_by_stage_only_touches_that_stage():
    store.create_application("u", "AppliedCo", None, status="Applied")
    store.create_application("u", "InterviewCo", None, status="Interview")
    handle_sms("u", "reject everything still in applied")
    handle_sms("u", "yes")
    assert store.find_application("u", "AppliedCo")["status"] == "Rejected"
    assert store.find_application("u", "InterviewCo")["status"] == "Interview"


def test_bulk_age_filter_excludes_recent():
    fresh = store.create_application("u", "FreshCo", None)
    old = store.create_application("u", "OldCo", None)
    _backdate(old["id"], 40)
    handle_sms("u", "ghost anything older than 30 days")
    handle_sms("u", "yes")
    assert store.find_application("u", "OldCo")["status"] == "Ghosted"
    assert store.find_application("u", "FreshCo")["status"] == "Applied"  # too recent


def test_bulk_never_touches_terminal_apps():
    store.create_application("u", "DoneCo", None, status="Offer")
    store.create_application("u", "OpenCo", None, status="Applied")
    reply = handle_sms("u", "reject everything in applied")
    # Only OpenCo matches; Offer is terminal and excluded.
    assert "1" in reply
    handle_sms("u", "yes")
    assert store.find_application("u", "DoneCo")["status"] == "Offer"


def test_bulk_nothing_matches():
    store.create_application("u", "InterviewCo", None, status="Interview")
    reply = handle_sms("u", "reject everything still in applied")
    assert "nothing matches" in reply.lower()
