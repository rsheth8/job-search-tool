"""TRACK / JOBS / PROFILE — heuristic routing + engine wiring (offline)."""
from __future__ import annotations

import pytest

from app import discovery, jobstore, profile
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


@pytest.mark.parametrize("text,company,msg", [
    ("apply 2", None, "2"),
    ("apply to #5", None, "5"),
    ("apply to the stripe one", "Stripe", None),
])
def test_apply_job_routes(text, company, msg):
    p = R.parse(text)
    assert p.intent == Intent.APPLY_JOB
    assert p.message == msg
    if company:
        assert p.company == company


def test_past_tense_applied_stays_apply():
    # "applied …" (past tense, logging an outside job) must NOT become APPLY_JOB.
    assert R.parse("applied stripe swe").intent == Intent.APPLY
    assert R.parse("applied to a job at google").intent == Intent.APPLY


# --- engine wiring ---------------------------------------------------------

def test_track_add_list_remove(monkeypatch):
    monkeypatch.setattr(
        "app.discovery.resolve_board",
        lambda company: {"source": "greenhouse", "board_token": "stripe",
                         "company_name": "Stripe", "count": 5},
    )
    monkeypatch.setattr("app.discovery.fetch_source", lambda s, t: [])  # nothing to seed
    reply = handle_sms("u", "track openings at stripe")
    assert "Tracking Stripe" in reply
    assert len(jobstore.list_tracked("u")) == 1

    listed = handle_sms("u", "what am i tracking")
    assert "Stripe" in listed

    removed = handle_sms("u", "stop tracking stripe")
    assert "Stopped tracking" in removed
    assert jobstore.list_tracked("u") == []


def test_track_baselines_existing_then_alerts_only_new(monkeypatch):
    feed = [JobPosting("greenhouse", "1", "Security Engineer", "https://x/1",
                       company="Ramp", description="security cloud")]
    monkeypatch.setattr(
        "app.discovery.resolve_board",
        lambda c: {"source": "greenhouse", "board_token": "ramp",
                   "company_name": "Ramp", "count": 1},
    )
    monkeypatch.setattr("app.discovery.fetch_source", lambda s, t: feed)
    profile.set_profile("u", roles="security engineer", keywords="security, cloud")

    reply = handle_sms("u", "track openings at ramp")
    assert "Baselined 1" in reply
    # The existing role is seeded (seen), NOT alerted — no first-track spam.
    assert jobstore.counts_by_status("u").get("seeded") == 1

    class Cap:
        def __init__(self): self.sent = []
        def send(self, u, b): self.sent.append(b)

    cap = Cap()
    assert discovery.tick("u", sender=cap) == 0  # nothing new since baseline

    # A genuinely new posting appears → it alerts.
    feed.append(JobPosting("greenhouse", "2", "Cloud Security Engineer", "https://x/2",
                           company="Ramp", description="security cloud"))
    assert discovery.tick("u", sender=cap) == 1


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


# --- assisted apply (Phase 2) ----------------------------------------------

def test_apply_job_logs_application_and_marks_posting():
    from app import store

    row = jobstore.save_posting(
        "u",
        JobPosting("greenhouse", "1", "Backend Engineer", "https://x/1",
                   company="Acme", location="Remote", description="python, apis"),
        relevance_score=0.82, status="alerted",
    )
    reply = handle_sms("u", f"apply {row['id']}")

    # Reply hands back the apply link + a draft, and confirms it logged.
    assert "https://x/1" in reply
    assert "Backend Engineer" in reply
    assert "Applied" in reply

    # The application is logged from discovery…
    apps = store.list_applications("u")
    assert any(a["company"] == "Acme" and a["source"] == "discovery" for a in apps)
    # …and the posting flips to 'applied' (so it won't re-alert / re-list).
    assert jobstore.get_posting("u", row["id"])["status"] == "applied"


def test_apply_job_unknown_id():
    reply = handle_sms("u", "apply 999")
    assert "don't have job #999" in reply


def test_apply_job_by_company():
    from app import store

    jobstore.save_posting(
        "u",
        JobPosting("lever", "9", "Data Engineer", "https://x/9", company="Ramp"),
        relevance_score=0.7, status="alerted",
    )
    reply = handle_sms("u", "apply to the ramp one")
    assert "https://x/9" in reply
    assert any(a["company"] == "Ramp" for a in store.list_applications("u"))


def test_apply_job_already_applied_is_idempotent_message():
    row = jobstore.save_posting(
        "u",
        JobPosting("greenhouse", "1", "SRE", "https://x/1", company="Acme"),
        relevance_score=0.9, status="alerted",
    )
    handle_sms("u", f"apply {row['id']}")
    again = handle_sms("u", f"apply {row['id']}")
    assert "already applied" in again.lower()
