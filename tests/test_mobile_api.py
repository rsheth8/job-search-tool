"""The endpoints the iOS app needs for Apply / Chat / Settings parity.

The phone browses matches, sees why each surfaced, and manages knowledge and the
apply queue. These tests pin that the payloads carry what the app renders.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import applicant, apply_queue, jobstore, knowledge, profile
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
    assert row["apply_kind"] == "autofill"
    assert row["apply_today"] is True


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


def test_apply_data_drops_listings_the_ats_json_says_are_gone(client, monkeypatch):
    from app import config
    from app.jobsources.alive import FetchResult

    _profile()
    row = _posting()
    monkeypatch.setenv("JOB_VERIFY_APPLY_URLS", "true")
    config.get_settings.cache_clear()
    monkeypatch.setattr(
        "app.jobsources.alive.http_get",
        lambda url, timeout=None: FetchResult(404, "", url),
    )
    body = client.get("/apply/data?user=u1").json()
    assert body["queued"] == []
    assert jobstore.get_posting("u1", row["id"])["status"] == "closed"


def test_apply_data_marks_top_five_apply_today(client):
    _profile()
    for i in range(7):
        _posting(ext=str(i), title=f"Backend Engineer {i}")
    rows = client.get("/apply/data?user=u1").json()["queued"]
    assert len(rows) == 7
    assert sum(1 for r in rows if r["apply_today"]) == 5
    assert all(r["apply_today"] for r in rows[:5])
    assert not any(r["apply_today"] for r in rows[5:])


def test_apply_data_still_works_when_a_posting_vanishes(client):
    """Rows are explained by re-reading the posting; a deleted one must not 500."""
    _profile()
    posting = _posting()
    apply_queue.stage("u1", posting["id"])
    jobstore.mark_posting_status("u1", posting["id"], "dismissed")
    assert client.get("/apply/data?user=u1").status_code == 200


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
    assert audit["score"] == round(5 / len(knowledge._IMPORTANT_IDENTITY), 2)
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


def test_snooze_hides_from_matches_and_queue(client):
    _profile()
    posting = _posting()
    apply_queue.stage("u1", posting["id"])
    r = client.post("/apply/snooze", json={"user": "u1", "posting_id": posting["id"]})
    assert r.status_code == 200 and r.json()["ok"] is True
    body = client.get("/apply/data?user=u1").json()
    assert body["queue"] == []
    assert all(row["posting_id"] != posting["id"] for row in body["queued"])
    assert jobstore.get_posting("u1", posting["id"])["status"] == "snoozed"


def test_apply_data_wakes_expired_snooze(client):
    from datetime import datetime, timedelta, timezone
    _profile()
    posting = _posting()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    jobstore.snooze_posting("u1", posting["id"], past)
    body = client.get("/apply/data?user=u1").json()
    assert any(row["posting_id"] == posting["id"] for row in body["queued"])
    assert jobstore.get_posting("u1", posting["id"])["status"] == "queued"


def test_promote_endpoint_puts_item_first(client):
    _profile()
    a = _posting(ext="1")
    apply_queue.stage("u1", a["id"])
    b = _posting(ext="2")
    apply_queue.stage("u1", b["id"])
    assert client.post("/apply/promote",
                       json={"user": "u1", "posting_id": a["id"]}).json()["ok"]
    ids = [row["posting_id"] for row in client.get("/apply/data?user=u1").json()["queue"]]
    assert ids[0] == a["id"]


def test_reorder_matches_persists(client):
    _profile()
    a = _posting(ext="1", title="Backend Engineer")
    b = _posting(ext="2", title="Backend Engineer Two")
    c = _posting(ext="3", title="Backend Engineer Three")
    r = client.post("/apply/reorder", json={
        "user": "u1", "matches": [c["id"], a["id"], b["id"]],
    })
    assert r.json()["ok"] is True
    ids = [row["posting_id"] for row in client.get("/apply/data?user=u1").json()["queued"]]
    assert ids[:3] == [c["id"], a["id"], b["id"]]


def test_reorder_ready_via_endpoint(client):
    _profile()
    a = _posting(ext="1")
    b = _posting(ext="2")
    apply_queue.stage("u1", a["id"])
    apply_queue.stage("u1", b["id"])
    assert client.post("/apply/reorder", json={
        "user": "u1", "queue": [a["id"], b["id"]],
    }).json()["ok"]
    ids = [row["posting_id"] for row in client.get("/apply/data?user=u1").json()["queue"]]
    assert ids == [a["id"], b["id"]]


def test_applications_lists_filed_apps(client):
    from app import store
    store.create_application("u1", "Acme", "Backend Engineer", source="mobile")
    body = client.get("/apply/applications?user=u1").json()
    assert len(body["applications"]) == 1
    row = body["applications"][0]
    assert row["company"] == "Acme"
    assert row["role"] == "Backend Engineer"
    assert row["status"] == "Applied"


def test_applications_empty_for_a_new_user(client):
    assert client.get("/apply/applications?user=nobody").json()["applications"] == []


# --- token gating -----------------------------------------------------------

def test_mobile_endpoints_respect_the_apply_token(client, monkeypatch):
    """These carry personal data, so they sit behind the same token as the rest."""
    from app import config

    monkeypatch.setenv("APPLY_API_TOKEN", "s3cret")
    config.get_settings.cache_clear()
    try:
        for path in ("/apply/knowledge?user=u1", "/apply/rules"):
            assert client.get(path).status_code == 401, path
            assert client.get(path, headers={"X-Apply-Token": "s3cret"}
                              ).status_code == 200, path
    finally:
        config.get_settings.cache_clear()
