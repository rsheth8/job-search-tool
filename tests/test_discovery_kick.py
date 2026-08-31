"""On-demand discovery: quiz / pull-to-refresh kick, not the 10-minute poll."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import config, discovery, profile
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


# ---------------------------------------------------------------------------
# /health must make the remaining beta blockers observable from outside
# ---------------------------------------------------------------------------

def test_health_reports_whether_base_resumes_are_on_the_volume(monkeypatch, tmp_path):
    """Both of these fail *soft* at runtime — a missing .tex just skips
    tailoring, an incomplete APNS_* set makes push a no-op — so without this
    the only way to answer "did the upload land?" was to SSH in and look."""
    monkeypatch.setenv("RESUME_TAILOR_ENABLED", "true")
    monkeypatch.setenv("RESUME_TEX_DIR", str(tmp_path))
    config.get_settings.cache_clear()

    body = TestClient(app).get("/health").json()
    assert body["resume"]["enabled"] is True
    assert body["resume"]["bases"] == []
    assert sorted(body["resume"]["expected"]) == ["aiml.tex", "swe.tex"]

    (tmp_path / "swe.tex").write_text("% swe")
    (tmp_path / "aiml.tex").write_text("% aiml")
    body = TestClient(app).get("/health").json()
    assert sorted(body["resume"]["bases"]) == ["aiml.tex", "swe.tex"]


def test_health_resume_section_survives_an_unreadable_dir(monkeypatch):
    monkeypatch.setenv("RESUME_TEX_DIR", "/definitely/not/here")
    config.get_settings.cache_clear()
    body = TestClient(app).get("/health").json()
    assert body["resume"]["bases"] == []


def test_health_names_the_missing_apns_settings(monkeypatch):
    monkeypatch.setenv("PUSH_ENABLED", "true")
    monkeypatch.setenv("APNS_KEY_ID", "k")
    monkeypatch.setenv("APNS_TEAM_ID", "t")
    monkeypatch.setenv("APNS_BUNDLE_ID", "com.rahil.jobpilot")
    monkeypatch.setenv("APNS_KEY_PATH", "")
    config.get_settings.cache_clear()

    push = TestClient(app).get("/health").json()["push"]
    assert push["enabled"] is True
    assert push["active"] is False
    assert push["missing"] == ["APNS_KEY_PATH"]


def test_health_push_is_clean_once_every_apns_value_is_set(monkeypatch):
    for key, value in (("PUSH_ENABLED", "true"), ("APNS_KEY_ID", "k"),
                       ("APNS_TEAM_ID", "t"), ("APNS_BUNDLE_ID", "com.rahil.jobpilot"),
                       ("APNS_KEY_PATH", "/data/apns.p8"),
                       ("APNS_USE_SANDBOX", "false")):
        monkeypatch.setenv(key, value)
    config.get_settings.cache_clear()

    push = TestClient(app).get("/health").json()["push"]
    assert push["active"] is True
    assert push["missing"] == []
    assert push["sandbox"] is False
