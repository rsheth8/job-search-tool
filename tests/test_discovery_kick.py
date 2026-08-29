"""On-demand discovery: quiz / pull-to-refresh kick, not the 10-minute poll."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import discovery, profile
from app.main import app


def _client() -> TestClient:
    return TestClient(app)


def test_apply_data_includes_discovery_status():
    body = _client().get("/apply/data?user=u1").json()
    assert "discovery" in body
    assert body["discovery"]["searching"] is False
    assert body["discovery"]["last_finished_at"] is None


def test_discover_requires_a_profile():
    r = _client().post("/apply/discover", json={"user": "fresh"})
    assert r.status_code == 200
    assert r.json()["started"] is False
    assert r.json()["reason"] == "no_profile"


def test_discover_runs_a_tick(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(discovery, "tick", lambda uid, **k: calls.append(uid) or 0)
    profile.set_profile("u1", roles="swe", locations="nyc")
    r = _client().post("/apply/discover", json={"user": "u1"})
    assert r.json()["started"] is True
    assert r.json()["reason"] == "ok"
    assert calls == ["u1"]
    status = _client().get("/apply/data?user=u1").json()["discovery"]
    assert status["searching"] is False
    assert status["last_finished_at"]


def test_discover_cooldown_then_force(monkeypatch):
    monkeypatch.setattr(discovery, "tick", lambda uid, **k: 0)
    profile.set_profile("u1", roles="swe", locations="nyc")
    assert _client().post("/apply/discover", json={"user": "u1"}).json()["started"] is True
    again = _client().post("/apply/discover", json={"user": "u1"})
    assert again.json()["started"] is False
    assert again.json()["reason"] == "cooldown"
    forced = _client().post("/apply/discover", json={"user": "u1", "force": True})
    assert forced.json()["started"] is True


def test_apply_data_refresh_kicks(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(discovery, "tick", lambda uid, **k: calls.append(uid) or 0)
    profile.set_profile("u1", roles="swe", locations="nyc")
    _client().get("/apply/data?user=u1&refresh=1")
    assert calls == ["u1"]


def test_profile_save_kicks_once_roles_exist(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(discovery, "tick", lambda uid, **k: calls.append(uid) or 0)
    _client().post("/apply/profile", json={
        "user": "u1", "fields": {"roles": "SWE intern", "locations": "remote"},
    })
    assert calls == ["u1"]


def test_setup_complete_force_kicks(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(discovery, "tick", lambda uid, **k: calls.append(uid) or 0)
    profile.set_profile("u1", roles="swe", locations="nyc")
    r = _client().post("/apply/setup", json={"user": "u1", "action": "complete"})
    assert r.json()["onboarding"] == "complete"
    assert calls == ["u1"]
    assert "discovery" in r.json()


def test_health_beta_invite_ready_false_in_tests():
    info = _client().get("/health").json()
    assert info["beta"]["invite_ready"] is False
    assert info["auth"]["fail_open"] is True
    assert info["db_ok"] is True


def test_health_beta_invite_ready_when_fail_closed(monkeypatch):
    from app import config

    monkeypatch.setenv("AUTH_FAIL_OPEN", "false")
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "false")
    monkeypatch.setenv("AUTH_ALLOWED_EMAILS", "friend@example.com")
    config.get_settings.cache_clear()
    info = _client().get("/health").json()
    assert info["auth"]["fail_open"] is False
    assert info["auth"]["dev_login"] is False
    assert info["auth"]["allowlist"] is True
    assert info["reminder_delivery"] == "app"
    assert info["beta"]["invite_ready"] is True
