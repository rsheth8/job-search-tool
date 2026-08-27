"""The endpoints the iOS app needs to reach parity with Slack and the dashboard.

Before these, the phone could only apply to things already staged from Slack: it
couldn't browse matches, see *why* one surfaced, approve a filled application, or
add a fact about you. Each of those existed on another surface already, so this is
about exposing the same data — the tests pin that the payloads carry what the app
renders, and that the approval gate stays exactly as strict here as everywhere else.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import applicant, apply_queue, fill_requests, jobstore, knowledge, profile
from app.jobsources import JobPosting
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def _posting(user="u1", ext="1", title="Backend Engineer", company="Acme",
             description="Build backend services in Python and Go.", location="Remote"):
    return jobstore.save_posting(user, JobPosting(
        source="greenhouse", external_id=ext, title=title,
        url="https://boards.greenhouse.io/acme/jobs/1", company=company,
        location=location, description=description),
        relevance_score=0.87, status="queued")


def _profile(user="u1"):
    profile.set_profile(user, roles="backend engineer", keywords="python, go",
                        locations="chicago")


# --- fit explanations on the rows -------------------------------------------

def test_apply_data_explains_why_each_match_surfaced(client):
    _profile()
    _posting()
    body = client.get("/apply/data?user=u1").json()
    row = body["queued"][0]
    assert row["why"].startswith("87% ·")
    assert "backend engineer" in " ".join(row["reasons"])
    assert "python" in " ".join(row["reasons"])
    assert row["concerns"] == []
    # un-staged matches must carry the same fillability flag as the queue —
    # otherwise the phone shows "Aggregator" on every Greenhouse link.
    assert row["auto_fillable"] is True


def test_staged_rows_are_explained_too(client):
    _profile()
    posting = _posting()
    apply_queue.stage("u1", posting["id"])
    body = client.get("/apply/data?user=u1").json()
    assert body["queued"] == []                     # moved out of the un-staged list
    assert body["queue"][0]["why"].startswith("87%")
    # the fields the app already relied on are still there
    assert body["queue"][0]["posting_id"] == posting["id"]
    assert body["queue"][0]["auto_fillable"] is True


def test_explanation_surfaces_concerns(client):
    _profile()
    _posting(title="Marketing Manager", location="Austin, TX",
             description="Own our campaigns.")
    row = client.get("/apply/data?user=u1").json()["queued"][0]
    assert row["concerns"], "a mismatched role should say so"


def test_apply_data_survives_an_empty_profile(client):
    _posting()
    row = client.get("/apply/data?user=u1").json()["queued"][0]
    assert row["why"]                                # still a line, just no reasons


def test_apply_data_still_works_when_a_posting_vanishes(client):
    """Rows are explained by re-reading the posting; a deleted one must not 500."""
    _profile()
    posting = _posting()
    apply_queue.stage("u1", posting["id"])
    jobstore.mark_posting_status(posting["id"], "dismissed")
    assert client.get("/apply/data?user=u1").status_code == 200


# --- in flight --------------------------------------------------------------

def test_inflight_lists_what_is_waiting_on_you(client):
    posting = _posting()
    req = fill_requests.create("u1", posting["id"])
    fill_requests.claim_next()
    fill_requests.set_preview(req["id"], {
        "filled": [{"label": "Email", "value": "a@b.c"}], "skipped": ["Gender"]})

    rows = client.get("/apply/inflight?user=u1").json()["inflight"]
    assert len(rows) == 1
    assert rows[0]["request_id"] == req["id"]
    assert rows[0]["status"] == "preview"
    assert rows[0]["awaiting"] is True
    assert "Backend Engineer @ Acme" in rows[0]["label"]
    # the preview rides along so the phone can review without a second round-trip
    assert rows[0]["preview"]["filled"][0]["label"] == "Email"
    assert rows[0]["preview"]["skipped"] == ["Gender"]


def test_inflight_is_empty_once_work_finishes(client):
    posting = _posting()
    req = fill_requests.create("u1", posting["id"])
    fill_requests.cancel("u1", req["id"])
    assert client.get("/apply/inflight?user=u1").json()["inflight"] == []


def test_approving_from_the_phone_uses_the_same_gate(client):
    """The mobile path must not get a shortcut around the human approval rule."""
    posting = _posting()
    req = fill_requests.create("u1", posting["id"])
    fill_requests.claim_next()

    # still filling — approving now must not advance it
    client.post("/apply/request/approve", json={"user": "u1", "request_id": req["id"]})
    assert fill_requests.get(req["id"])["status"] == fill_requests.FILLING

    fill_requests.set_preview(req["id"], {"filled": [], "skipped": []})
    client.post("/apply/request/approve", json={"user": "u1", "request_id": req["id"]})
    assert fill_requests.get(req["id"])["status"] == fill_requests.APPROVED


def test_a_different_user_cannot_approve_your_fill(client):
    posting = _posting()
    req = fill_requests.create("u1", posting["id"])
    fill_requests.claim_next()
    fill_requests.set_preview(req["id"], {"filled": [], "skipped": []})
    client.post("/apply/request/approve", json={"user": "u2", "request_id": req["id"]})
    assert fill_requests.get(req["id"])["status"] == fill_requests.PREVIEW


# --- knowledge --------------------------------------------------------------

def test_knowledge_round_trip(client):
    r = client.post("/apply/knowledge", json={
        "user": "u1", "category": "project",
        "text": "Built a real-time pricing service in Go"})
    assert r.json()["ok"] is True

    body = client.get("/apply/knowledge?user=u1").json()
    assert len(body["items"]) == 1
    assert "pricing service" in body["items"][0]["text"]
    assert body["audit"]["identity_missing"]          # nothing filled in yet


def test_knowledge_rejects_an_unknown_category(client):
    r = client.post("/apply/knowledge", json={
        "user": "u1", "category": "nonsense", "text": "x"})
    assert r.json()["ok"] is False
    assert client.get("/apply/knowledge?user=u1").json()["items"] == []


def test_knowledge_saves_a_reusable_answer(client):
    client.post("/apply/knowledge", json={
        "user": "u1", "category": "answer", "label": "Why do you want to work here?",
        "text": "I care about infrastructure at scale."})
    item = client.get("/apply/knowledge?user=u1").json()["items"][0]
    assert item["label"] == "Why do you want to work here?"
    # and it's immediately reusable with no model call
    assert knowledge.canned_answer("u1", "Why do you want to work at Acme?")


def test_knowledge_remove(client):
    item = client.post("/apply/knowledge", json={
        "user": "u1", "category": "strength", "text": "Systems debugging"}).json()["item"]
    assert client.post("/apply/knowledge/remove",
                       json={"user": "u1", "id": item["id"]}).json()["ok"] is True
    assert client.get("/apply/knowledge?user=u1").json()["items"] == []


def test_audit_reflects_a_filled_in_identity(client):
    applicant.set_identity("u1", {
        "first_name": "Rahil", "last_name": "Sheth", "email": "r@example.com",
        "phone": "555-0100", "city": "Chicago", "state": "IL"})
    audit = client.get("/apply/knowledge?user=u1").json()["audit"]
    assert audit["score"] > 0.3
    assert "email" in audit["identity_have"]


# --- skip / pass from the phone ---------------------------------------------

def test_pass_unstages_and_dismisses(client):
    """Phone 'Pass' should clear the ready queue *and* drop it from matches."""
    _profile()
    posting = _posting()
    apply_queue.stage("u1", posting["id"])
    assert client.get("/apply/data?user=u1").json()["queue"]

    r = client.post("/apply/pass", json={"user": "u1", "posting_id": posting["id"]})
    assert r.status_code == 200 and r.json()["ok"] is True
    body = client.get("/apply/data?user=u1").json()
    assert body["queue"] == []
    assert all(row["posting_id"] != posting["id"] for row in body["queued"])
    assert jobstore.get_posting("u1", posting["id"])["status"] == "dismissed"


def test_remove_only_unstages(client):
    """Skip-for-now keeps the posting available as a top match."""
    _profile()
    posting = _posting()
    apply_queue.stage("u1", posting["id"])
    assert client.post("/apply/remove",
                       json={"user": "u1", "posting_id": posting["id"]}).json()["ok"]
    body = client.get("/apply/data?user=u1").json()
    assert body["queue"] == []
    assert any(row["posting_id"] == posting["id"] for row in body["queued"])


# --- token gating -----------------------------------------------------------

def test_mobile_endpoints_respect_the_apply_token(client, monkeypatch):
    """These carry personal data, so they sit behind the same token as the rest."""
    from app import config

    monkeypatch.setenv("APPLY_API_TOKEN", "s3cret")
    config.get_settings.cache_clear()
    try:
        for path in ("/apply/inflight?user=u1", "/apply/knowledge?user=u1",
                     "/apply/rules"):
            assert client.get(path).status_code == 401, path
            assert client.get(path, headers={"X-Apply-Token": "s3cret"}
                              ).status_code == 200, path
    finally:
        config.get_settings.cache_clear()
