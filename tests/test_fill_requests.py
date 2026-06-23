"""Phase 2 submit pipeline: fill-request state machine + worker/user endpoints.
The browser worker itself isn't tested here (it drives a live browser); this
covers the relay it talks to."""
from __future__ import annotations

from app import apply_queue, fill_requests, jobstore
from app.jobsources import JobPosting


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _posting(ext="1"):
    # A first-party ATS URL so /apply/autosubmit treats it as auto-fillable.
    return JobPosting("greenhouse", ext, "Software Engineer",
                      "https://boards.greenhouse.io/acme/jobs/1",
                      company="Acme", location="Remote", description="python")


def _save(user="u1"):
    return jobstore.save_posting(user, _posting(), relevance_score=0.8,
                                 status="queued")["id"]


# --- state machine ---------------------------------------------------------

def test_full_lifecycle():
    pid = _save()
    req = fill_requests.create("u1", pid)
    assert req["status"] == "pending"
    # Idempotent while active.
    assert fill_requests.create("u1", pid)["id"] == req["id"]

    claimed = fill_requests.claim_next()
    assert claimed["id"] == req["id"] and claimed["status"] == "filling"
    assert fill_requests.claim_next() is None          # nothing else pending

    assert fill_requests.set_preview(req["id"], {"filled": [{"label": "Email", "value": "a@x"}]})
    assert fill_requests.for_posting("u1", pid)["status"] == "preview"

    # Approval is owner-scoped and only from 'preview'.
    assert fill_requests.approve("someone-else", req["id"]) is False
    assert fill_requests.approve("u1", req["id"]) is True

    sub = fill_requests.claim_approved()
    assert sub["id"] == req["id"] and sub["status"] == "submitting"
    assert fill_requests.mark_submitted(req["id"]) is True
    assert fill_requests.get(req["id"])["status"] == "submitted"


def test_cannot_approve_before_preview():
    pid = _save()
    req = fill_requests.create("u1", pid)
    assert fill_requests.approve("u1", req["id"]) is False   # still pending


def test_cancel_and_failure():
    pid = _save()
    req = fill_requests.create("u1", pid)
    assert fill_requests.cancel("u1", req["id"]) is True
    assert fill_requests.get(req["id"])["status"] == "failed"
    # A fresh request can be created again after cancel.
    assert fill_requests.create("u1", pid)["id"] != req["id"]


def test_preview_roundtrips_as_dict():
    pid = _save()
    req = fill_requests.create("u1", pid)
    fill_requests.claim_next()
    fill_requests.set_preview(req["id"], {"filled": [], "skipped": ["Resume"]})
    assert fill_requests.get(req["id"])["preview"] == {"filled": [], "skipped": ["Resume"]}


# --- endpoints -------------------------------------------------------------

def test_autosubmit_then_worker_claim_carries_package():
    from app import applicant
    applicant.set_identity("u1", {"email": "ada@x.com", "city": "Chicago", "state": "IL"})
    pid = _save()
    c = _client()
    r = c.post("/apply/autosubmit", json={"user": "u1", "posting_id": pid}).json()
    assert r["status"] == "pending"

    job = c.post("/worker/claim").json()
    assert job["posting_id"] == pid and job["url"] == "https://boards.greenhouse.io/acme/jobs/1"
    assert job["identity"]["email"] == "ada@x.com"
    assert job["questions"]                       # per-question answers travel too


def test_autosubmit_non_fillable_url_hands_off():
    """An aggregator/non-ATS URL is not auto-filled: the endpoint reports it as
    not fillable and creates no worker request."""
    p = JobPosting("aggregator", "agg1", "SWE",
                   "https://careersprint.7f.liveblog365.com/job/1?utm_campaign=google_jobs_apply",
                   company="Acme", location="Remote", description="python")
    pid = jobstore.save_posting("u1", p, relevance_score=0.8, status="queued")["id"]
    c = _client()
    r = c.post("/apply/autosubmit", json={"user": "u1", "posting_id": pid}).json()
    assert r["fillable"] is False and "request_id" not in r
    assert c.post("/worker/claim").json() == {}   # nothing queued for the worker


def test_worker_preview_then_user_approve_then_submit_logs_application():
    from app import store
    pid = _save()
    c = _client()
    rid = c.post("/apply/autosubmit", json={"user": "u1", "posting_id": pid}).json()["request_id"]
    c.post("/worker/claim")
    c.post("/worker/preview", json={"request_id": rid,
                                    "preview": {"filled": [{"label": "Email", "value": "a@x"}]}})

    # User sees the preview.
    req = c.get(f"/apply/request?user=u1&posting_id={pid}").json()["request"]
    assert req["status"] == "preview" and req["preview"]["filled"]

    # Approve, worker submits.
    assert c.post("/apply/request/approve", json={"user": "u1", "request_id": rid}).json()["ok"]
    c.post("/worker/claim_approved")
    c.post("/worker/result", json={"request_id": rid, "status": "submitted"})

    # The application is logged and the posting/queue marked.
    assert any(a["company"] == "Acme" for a in store.list_applications("u1"))
    assert jobstore.get_posting("u1", pid)["status"] == "applied"


def test_worker_endpoints_require_token(monkeypatch):
    monkeypatch.setenv("APPLY_API_TOKEN", "secret")
    from app.config import get_settings
    get_settings.cache_clear()
    c = _client()
    assert c.post("/worker/claim").status_code == 401
    assert c.post("/worker/claim", headers={"X-Apply-Token": "secret"}).status_code == 200
    get_settings.cache_clear()
