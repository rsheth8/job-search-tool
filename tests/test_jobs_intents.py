"""TRACK / JOBS / PROFILE — heuristic routing + engine wiring (offline)."""
from __future__ import annotations

import pytest

from app import jobstore, profile
from app.engine import handle_sms
from app.intents import Intent
from app.jobsources import JobPosting
from app.router import HeuristicRouter

R = HeuristicRouter()


# --- routing ---------------------------------------------------------------

@pytest.mark.parametrize("text,intent,msg", [
    ("track openings at stripe", Intent.TRACK, None),
    ("watch figma", Intent.TRACK, None),
    ("stop tracking databricks", Intent.TRACK, "remove"),
    ("what am i tracking", Intent.TRACK, "list"),
    ("any new jobs", Intent.JOBS, None),
    ("show me openings", Intent.JOBS, None),
    ("show my profile", Intent.PROFILE, None),
])
def test_routes(text, intent, msg):
    p = R.parse(text)
    assert p.intent == intent
    if msg is not None:
        assert p.message == msg


def test_track_extracts_company():
    assert R.parse("track openings at stripe").company == "Stripe"
    assert R.parse("stop tracking databricks").company == "Databricks"


def test_profile_set_keeps_criteria():
    p = R.parse("i'm looking for new grad swe roles, remote or nyc")
    assert p.intent == Intent.PROFILE
    assert "swe" in (p.message or "").lower()


def test_apply_not_hijacked_by_jobs_keyword():
    # "job" inside an apply message must stay APPLY, not JOBS.
    assert R.parse("applied to a job at google").intent == Intent.APPLY


# --- engine wiring ---------------------------------------------------------

def test_track_add_list_remove(monkeypatch):
    monkeypatch.setattr(
        "app.discovery.resolve_board",
        lambda company: {"source": "greenhouse", "board_token": "stripe",
                         "company_name": "Stripe", "count": 5},
    )
    reply = handle_sms("u", "track openings at stripe")
    assert "Tracking Stripe" in reply
    assert len(jobstore.list_tracked("u")) == 1

    listed = handle_sms("u", "what am i tracking")
    assert "Stripe" in listed

    removed = handle_sms("u", "stop tracking stripe")
    assert "Stopped tracking" in removed
    assert jobstore.list_tracked("u") == []


def test_track_unknown_company(monkeypatch):
    monkeypatch.setattr("app.discovery.resolve_board", lambda company: None)
    reply = handle_sms("u", "track openings at nonexistentco")
    assert "Couldn't find" in reply


def test_profile_set_then_show():
    set_reply = handle_sms("u", "looking for new grad swe roles, remote or nyc")
    assert "match new jobs" in set_reply.lower()
    row = profile.get_profile("u")
    assert "swe" in (row["roles"] or "").lower()
    assert "remote" in (row["locations"] or "")
    assert "nyc" in (row["locations"] or "")

    show_reply = handle_sms("u", "show my profile")
    assert "profile" in show_reply.lower()
    assert "Roles" in show_reply


def test_jobs_listing_states(monkeypatch):
    # No boards tracked yet.
    assert "not tracking" in handle_sms("u", "any new jobs").lower()

    # Tracking but nothing surfaced.
    jobstore.add_tracked_company("u", "greenhouse", "acme", "Acme")
    assert "no new matching jobs" in handle_sms("u", "any new jobs").lower()

    # A surfaced posting shows up.
    jobstore.save_posting(
        "u",
        JobPosting("greenhouse", "1", "Backend Engineer", "https://x/1",
                   company="Acme", location="Remote"),
        relevance_score=0.82, status="alerted",
    )
    reply = handle_sms("u", "any new jobs")
    assert "Backend Engineer" in reply and "82%" in reply
