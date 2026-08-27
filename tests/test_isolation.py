"""Invite-only isolation: sessions, allowlist, fail-closed prod gate."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import applicant, apply_queue, auth, config, jobstore, knowledge, profile
from app.jobsources import JobPosting
from app.main import app


def _client() -> TestClient:
    return TestClient(app)


def _login(c: TestClient, user_id: str, monkeypatch, *, display="T") -> dict:
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    config.get_settings.cache_clear()
    return c.post("/auth/dev", json={"user_id": user_id, "display_name": display}).json()


def test_unauthenticated_apply_data_is_401_when_fail_closed(monkeypatch):
    monkeypatch.setenv("AUTH_FAIL_OPEN", "false")
    config.get_settings.cache_clear()
    assert _client().get("/apply/data?user=u1").status_code == 401
    assert _client().get("/").status_code == 401
    assert _client().get("/apply").status_code == 401
    assert _client().get("/train").status_code == 401


def test_session_user_a_cannot_read_user_b(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    config.get_settings.cache_clear()
    c = _client()
    a = _login(c, "usr_alice", monkeypatch)
    b = _login(c, "usr_bob", monkeypatch)

    profile.set_profile("usr_alice", roles="backend", keywords="python", locations="nyc")
    posting = jobstore.save_posting(
        "usr_alice",
        JobPosting(
            source="greenhouse", external_id="1", title="Backend",
            url="https://boards.greenhouse.io/acme/jobs/1", company="Acme",
            location="NYC", description="Python",
        ),
        relevance_score=0.9, status="queued",
    )
    apply_queue.stage("usr_alice", posting["id"])
    knowledge.add("usr_alice", "project", "Alice's secret project")
    applicant.set_identity("usr_alice", {"email": "alice@example.com"})

    headers_b = {"Authorization": f"Bearer {b['token']}"}
    # Even if Bob asks for Alice's user id, the session wins.
    data = c.get("/apply/data?user=usr_alice", headers=headers_b).json()
    assert data["user"] == "usr_bob"
    assert data["queue"] == []
    know = c.get("/apply/knowledge?user=usr_alice", headers=headers_b).json()
    assert know["user"] == "usr_bob"
    assert know["items"] == []
    ident = c.get("/apply/identity?user=usr_alice", headers=headers_b).json()
    assert ident["user"] == "usr_bob"
    assert ident["fields"].get("email") != "alice@example.com"

    headers_a = {"Authorization": f"Bearer {a['token']}"}
    mine = c.get("/apply/data", headers=headers_a).json()
    assert mine["user"] == "usr_alice"
    assert mine["queue"]


def test_allowlist_rejects_unknown_email(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOWED_EMAILS", "friend@example.com")
    config.get_settings.cache_clear()

    monkeypatch.setattr(
        auth, "verify_apple_identity_token",
        lambda token: {"sub": "apple-sub-stranger", "email": "stranger@x.com"},
    )
    res = _client().post("/auth/apple", json={"identity_token": "fake"})
    assert res.status_code == 403
    assert "invite-only" in res.json()["detail"]


def test_allowlist_accepts_listed_email(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOWED_EMAILS", "friend@example.com, other@x.com")
    config.get_settings.cache_clear()

    monkeypatch.setattr(
        auth, "verify_apple_identity_token",
        lambda token: {"sub": "apple-sub-friend", "email": "friend@example.com"},
    )
    res = _client().post("/auth/apple", json={"identity_token": "fake"})
    assert res.status_code == 200
    assert res.json()["token"]
    assert res.json()["user"]["email"] == "friend@example.com"


def test_allowlist_empty_allows_anyone(monkeypatch):
    config.get_settings.cache_clear()
    monkeypatch.setattr(
        auth, "verify_apple_identity_token",
        lambda token: {"sub": "apple-sub-any", "email": "anyone@x.com"},
    )
    assert _client().post("/auth/apple", json={"identity_token": "fake"}).status_code == 200


def test_html_dashboard_session_ignores_query_user(monkeypatch):
    monkeypatch.setenv("AUTH_FAIL_OPEN", "false")
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    config.get_settings.cache_clear()
    c = _client()
    from app import store

    store.create_application("usr_alice", "AliceCo", "SWE")
    store.create_application("usr_bob", "BobCo", "PM")
    tok = c.post("/auth/dev", json={"user_id": "usr_alice"}).json()["token"]
    html = c.get("/?user=usr_bob", headers={"Authorization": f"Bearer {tok}"}).text
    assert "AliceCo" in html
    assert "BobCo" not in html
