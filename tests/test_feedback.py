"""POST /feedback stores a row for the signed-in user."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import config, feedback
from app.main import app


def test_feedback_requires_auth():
    assert TestClient(app).post("/feedback", json={"body": "hi"}).status_code == 401


def test_feedback_round_trip(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    config.get_settings.cache_clear()
    c = TestClient(app)
    tok = c.post("/auth/dev", json={"user_id": "usr_fb"}).json()["token"]
    r = c.post(
        "/feedback",
        headers={"Authorization": f"Bearer {tok}"},
        json={"body": "Autofill skipped the school field on Greenhouse."},
    )
    assert r.status_code == 200
    rows = feedback.list_recent()
    assert any("school field" in x["body"] for x in rows)
    assert rows[0]["user_id"] == "usr_fb"


def test_feedback_rejects_empty(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    config.get_settings.cache_clear()
    c = TestClient(app)
    tok = c.post("/auth/dev", json={"user_id": "usr_fb"}).json()["token"]
    assert c.post(
        "/feedback",
        headers={"Authorization": f"Bearer {tok}"},
        json={"body": "  "},
    ).status_code == 400
