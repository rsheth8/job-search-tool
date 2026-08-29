"""Launch-readiness: request IDs, safe 500s, health, feedback context, chat fail-open."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import chat, config, feedback
from app.main import app


def _client() -> TestClient:
    return TestClient(app)


def test_every_response_has_a_request_id():
    r = _client().get("/health")
    assert r.status_code == 200
    rid = r.headers.get("X-Request-Id")
    assert rid and len(rid) >= 8


def test_client_request_id_is_echoed():
    r = _client().get("/health", headers={"X-Request-Id": "tester-abc123"})
    assert r.headers.get("X-Request-Id") == "tester-abc123"


def test_health_pings_the_database():
    info = _client().get("/health").json()
    assert info["db_ok"] is True
    assert info["status"] == "ok"


def test_401_is_json_with_code_and_request_id():
    r = _client().get("/chat/history")
    assert r.status_code == 401
    body = r.json()
    assert body["detail"] == "sign in required"
    assert body["code"] == "auth_required"
    assert body["request_id"]
    assert r.headers.get("X-Request-Id") == body["request_id"]


def test_unhandled_error_does_not_leak_and_keeps_a_request_id(monkeypatch):
    from app import apply_queue

    def boom(*_a, **_k):
        raise RuntimeError("secret stack / tmp/keys")

    monkeypatch.setattr(apply_queue, "get_package", boom)
    r = _client().post("/apply/package", json={"user": "u1", "posting_id": 1})
    assert r.status_code == 500
    body = r.json()
    blob = str(body).lower()
    assert "secret" not in blob
    assert "traceback" not in blob
    assert "tmp/keys" not in blob
    assert body["code"] == "internal_error"
    assert body["request_id"]
    assert "Settings" in body["detail"]
    assert r.headers.get("X-Request-Id") == body["request_id"]


def test_package_missing_is_404_not_a_200_error_object():
    r = _client().post("/apply/package", json={"user": "u1", "posting_id": 99999})
    assert r.status_code == 404
    assert "ready" in r.json()["detail"].lower()
    assert r.json()["code"] == "not_found"


def test_chat_survives_an_engine_crash(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    config.get_settings.cache_clear()

    def boom(*_a, **_k):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(chat, "handle_sms", boom)
    c = _client()
    tok = c.post("/auth/dev", json={"user_id": "usr_err"}).json()["token"]
    r = c.post("/chat", headers={"Authorization": f"Bearer {tok}"}, json={"text": "stats"})
    assert r.status_code == 200
    assert "wrong" in r.json()["reply"].lower() or "try again" in r.json()["reply"].lower()
    hist = c.get("/chat/history", headers={"Authorization": f"Bearer {tok}"}).json()["messages"]
    assert len(hist) >= 2
    assert hist[-1]["role"] == "assistant"


def test_feedback_stores_diagnostics_context(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    config.get_settings.cache_clear()
    c = _client()
    tok = c.post("/auth/dev", json={"user_id": "usr_fbctx"}).json()["token"]
    r = c.post(
        "/feedback",
        headers={"Authorization": f"Bearer {tok}"},
        json={
            "body": "Fill missed school",
            "context": {
                "app_version": "0.1.0 (1)",
                "last_request_id": "abc123def456",
                "last_path": "/apply/package",
                "last_status": 500,
            },
        },
    )
    assert r.status_code == 200
    rows = feedback.list_recent(5)
    hit = next(x for x in rows if x["user_id"] == "usr_fbctx")
    assert "school" in hit["body"]
    assert "abc123def456" in (hit.get("context") or "")
