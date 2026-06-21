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
    ("review jobs", Intent.JOBS_REVIEW, None),
    ("let's go through the new jobs", Intent.JOBS_REVIEW, None),
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


@pytest.mark.parametrize("text,company,msg", [
    ("queue 3", None, "3"),
    ("stage #7", None, "7"),
    ("queue up 2", None, "2"),
    ("queue the stripe one", "Stripe", None),
])
def test_queue_job_routes(text, company, msg):
    p = R.parse(text)
    assert p.intent == Intent.QUEUE_JOB
    assert p.message == msg
    if company:
        assert p.company == company


def test_queue_job_stages_without_applying():
    from app import apply_queue, store

    row = jobstore.save_posting(
        "u",
        JobPosting("greenhouse", "1", "Backend Engineer", "https://x/1",
                   company="Acme", location="Remote", description="python, apis"),
        relevance_score=0.82, status="queued",
    )
    reply = handle_sms("u", f"queue {row['id']}")
    assert "/apply" in reply and "Backend Engineer" in reply

    # Staged into the apply queue…
    assert [it["posting_id"] for it in apply_queue.list_queue("u")] == [row["id"]]
    # …but NOT applied: no application logged, posting stays queued.
    assert store.list_applications("u") == []
    assert jobstore.get_posting("u", row["id"])["status"] == "queued"

    # Idempotent: queueing again just says it's already there.
    assert "already in your apply queue" in handle_sms("u", f"queue {row['id']}")


def test_queue_job_unknown_id():
    assert "don't have job #999" in handle_sms("u", "queue 999")


@pytest.mark.parametrize("text,spec", [
    ("queue top 3", "top:3"),
    ("stage the top 5", "top:5"),
    ("queue all", "all"),
])
def test_queue_bulk_routes(text, spec):
    p = R.parse(text)
    assert p.intent == Intent.QUEUE_JOB and p.message == spec


def test_queue_top_n_stages_best_matches():
    from app import apply_queue
    for i in range(4):
        jobstore.save_posting(
            "u", JobPosting("greenhouse", str(i), f"Engineer {i}", f"https://x/{i}",
                            company=f"Co{i}", location="Remote", description="python"),
            relevance_score=0.9 - i * 0.1, status="queued")
    reply = handle_sms("u", "queue top 2")
    assert "Staged 2" in reply
    # The two best-scored matches were staged.
    staged = {it["posting_id"] for it in apply_queue.list_queue("u")}
    assert len(staged) == 2
    # Running again stages the rest (idempotent on the already-staged).
    handle_sms("u", "queue all")
    assert len(apply_queue.list_queue("u")) == 4


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
    assert "match new jobs" in set_reply.lower() or "wide discovery" in set_reply.lower()
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
    assert "no jobs in your queue" in handle_sms("u", "any new jobs").lower()

    # A surfaced posting shows up.
    jobstore.save_posting(
        "u",
        JobPosting("greenhouse", "1", "Backend Engineer", "https://x/1",
                   company="Acme", location="Remote"),
        relevance_score=0.82, status="queued",
    )
    reply = handle_sms("u", "any new jobs")
    assert "Backend Engineer" in reply and "82%" in reply
    assert "queue" in reply.lower()


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


# --- dismiss / snooze (polish) ---------------------------------------------

@pytest.mark.parametrize("text,intent,msg", [
    ("dismiss 3", Intent.DISMISS_JOB, "3"),
    ("not interested in #4", Intent.DISMISS_JOB, "4"),
    ("hide the stripe one", Intent.DISMISS_JOB, None),
    ("snooze 5", Intent.SNOOZE_JOB, "5"),
    ("snooze 5 for a week", Intent.SNOOZE_JOB, "5"),
])
def test_dismiss_snooze_routes(text, intent, msg):
    p = R.parse(text)
    assert p.intent == intent
    assert p.message == msg


def _alerted(uid="u", ext="1", title="Backend Engineer", company="Acme"):
    # Track the board too, so an emptied listing says "no new matching jobs"
    # (the "not tracking anything" branch only fires with zero boards).
    jobstore.add_tracked_company(uid, "greenhouse", company.lower(), company)
    return jobstore.save_posting(
        uid, JobPosting("greenhouse", ext, title, f"https://x/{ext}", company=company),
        relevance_score=0.8, status="alerted",
    )


def test_dismiss_hides_posting_from_listing():
    row = _alerted()
    reply = handle_sms("u", f"dismiss {row['id']}")
    assert "Dismissed" in reply
    assert jobstore.get_posting("u", row["id"])["status"] == "dismissed"
    assert "no jobs in your queue" in handle_sms("u", "any new jobs").lower()


def test_snooze_then_resurfaces_after_expiry():
    from datetime import datetime, timedelta, timezone

    row = _alerted()
    reply = handle_sms("u", f"snooze {row['id']} for a week")
    assert "Snoozed" in reply
    assert jobstore.get_posting("u", row["id"])["status"] == "snoozed"
    # Hidden while snoozed.
    assert "no jobs in your queue" in handle_sms("u", "any new jobs").lower()
    # Force the snooze into the past → it wakes and shows again.
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    jobstore.snooze_posting(row["id"], past)
    assert "Backend Engineer" in handle_sms("u", "any new jobs")


def test_dismiss_unknown_id():
    assert "don't have job #42" in handle_sms("u", "dismiss 42")


# --- tune matching (polish) ------------------------------------------------

@pytest.mark.parametrize("text,msg", [
    ("only show me 80%+ matches", "set:0.8"),
    ("be less picky about matches", "loosen"),
    ("be more selective with jobs", "tighten"),
    ("reset my match threshold", "reset"),
])
def test_tune_routes(text, msg):
    p = R.parse(text)
    assert p.intent == Intent.TUNE
    assert p.message == msg


def test_tune_sets_threshold_value():
    profile.set_profile("u", roles="swe", keywords="swe")
    reply = handle_sms("u", "only show me 80%+ matches")
    assert "80%" in reply
    assert profile.get_profile("u")["min_relevance"] == 0.8


def test_tune_threshold_is_honored_in_tick(monkeypatch):
    # Two-term profile so a single-term hit scores ~0.5 (below an 0.8 bar).
    profile.set_profile("u", roles="swe, backend", keywords="swe, backend")
    profile.set_min_relevance("u", 0.8)
    jobstore.add_tracked_company("u", "greenhouse", "acme", "Acme")

    feed = [JobPosting("greenhouse", "1", "SWE", "https://x/1",
                       company="Acme", description="swe role")]
    monkeypatch.setattr("app.discovery.fetch_source", lambda s, t: feed)

    class Cap:
        def __init__(self): self.sent = []
        def send(self, u, b): self.sent.append(b)

    cap = Cap()
    # ~0.5 < 0.8 → stored but not alerted.
    assert discovery.tick("u", sender=cap) == 0
    assert cap.sent == []
    assert jobstore.get_posting("u", 1)["status"] == "new"

    # Drop the bar; a fresh full-match posting now alerts.
    profile.set_min_relevance("u", 0.4)
    feed.append(JobPosting("greenhouse", "2", "Backend SWE", "https://x/2",
                           company="Acme", description="swe backend"))
    assert discovery.tick("u", sender=cap) == 1


def test_tune_reset_clears_to_default():
    profile.set_min_relevance("u", 0.9)
    reply = handle_sms("u", "reset matching")
    assert "default" in reply.lower()
    assert profile.get_profile("u")["min_relevance"] is None


# --- richer tracked list (polish) ------------------------------------------

def test_tracking_list_shows_counts_and_threshold(monkeypatch):
    monkeypatch.setattr(
        "app.discovery.resolve_board",
        lambda c: {"source": "greenhouse", "board_token": "acme",
                   "company_name": "Acme", "count": 0},
    )
    monkeypatch.setattr("app.discovery.fetch_source", lambda s, t: [])
    handle_sms("u", "track openings at acme")
    jobstore.save_posting(
        "u", JobPosting("greenhouse", "1", "SWE", "https://x/1", company="Acme"),
        relevance_score=0.8, status="alerted",
    )
    listed = handle_sms("u", "what am i tracking")
    assert "Acme" in listed
    assert "seen" in listed and "new" in listed
    assert "Matching" in listed  # threshold line


def test_review_jobs_walkthrough(monkeypatch):
    jobstore.save_posting(
        "u",
        JobPosting("greenhouse", "1", "Role A", "https://x/1", company="Acme"),
        relevance_score=0.9, status="queued",
    )
    jobstore.save_posting(
        "u",
        JobPosting("greenhouse", "2", "Role B", "https://x/2", company="Acme"),
        relevance_score=0.8, status="queued",
    )
    start = handle_sms("u", "review jobs")
    assert "1 of 2" in start and "Role A" in start

    skipped = handle_sms("u", "skip")
    assert "Role B" in skipped and "1 of 1" in skipped

    applied = handle_sms("u", "apply")
    assert "Logged" in applied and "Role B" in applied
    assert jobstore.count_queued("u") == 0

    from app import store

    apps = store.list_applications("u")
    assert any("Role B" in (a["role"] or "") for a in apps)
