"""Auth + chat API tests (dev login; no live Apple)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import auth, chat, config, reminders
from app.main import app


def _client() -> TestClient:
    return TestClient(app)


def test_chat_requires_auth():
    c = _client()
    assert c.get("/chat/history").status_code == 401
    assert c.post("/chat", json={"text": "hi"}).status_code == 401


def test_dev_login_and_chat_roundtrip(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    config.get_settings.cache_clear()

    c = _client()
    res = c.post("/auth/dev", json={"display_name": "Test"})
    assert res.status_code == 200
    data = res.json()
    token = data["token"]
    uid = data["user"]["id"]
    assert uid.startswith("usr_")

    headers = {"Authorization": f"Bearer {token}"}
    me = c.get("/auth/me", headers=headers).json()
    assert me["user"]["id"] == uid

    sent = c.post("/chat", headers=headers, json={"text": "stats"})
    assert sent.status_code == 200
    body = sent.json()
    assert body["reply"]
    assert body["user_message"]["role"] == "user"
    assert body["assistant_message"]["role"] == "assistant"

    hist = c.get("/chat/history", headers=headers).json()
    assert len(hist["messages"]) >= 2
    assert hist["messages"][0]["role"] == "user"


def test_dev_login_disabled_by_default():
    c = _client()
    assert c.post("/auth/dev", json={}).status_code == 403


def test_session_resolves_apply_user(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    config.get_settings.cache_clear()

    c = _client()
    data = c.post("/auth/dev", json={"user_id": "usr_applytest"}).json()
    headers = {"Authorization": f"Bearer {data['token']}"}
    # Even with a different ?user=, session wins.
    res = c.get("/apply/data?user=someone-else", headers=headers)
    assert res.status_code == 200
    assert res.json()["user"] == "usr_applytest"


def test_legacy_migrate_on_first_dev_login(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    monkeypatch.setenv("AUTH_LEGACY_USER_ID", "U_LEGACY")
    config.get_settings.cache_clear()

    from app import knowledge

    knowledge.add("U_LEGACY", "project", "Built a search tool", label=None)

    c = _client()
    data = c.post("/auth/dev", json={}).json()
    uid = data["user"]["id"]
    items = knowledge.list_all(uid)
    assert any("search tool" in (i["text"] or "") for i in items)
    user = auth.get_user(uid)
    assert user["legacy_user_id"] == "U_LEGACY"


def test_app_sender_appends_chat(monkeypatch):
    config.get_settings.cache_clear()
    reminders._sender_singleton = None
    sender = reminders.get_sender()
    assert isinstance(sender, reminders.AppSender)
    sender.send("u_chat", "Reminder: follow up with Acme")
    msgs = chat.history("u_chat")
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant"
    assert "Acme" in msgs[0]["body"]


def test_slack_webhook_disabled_by_default():
    c = _client()
    res = c.post("/slack/events", json={"type": "url_verification", "challenge": "x"})
    assert res.status_code == 404


def test_slack_webhook_works_when_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("SLACK_TRANSPORT_ENABLED", "true")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "")
    config.get_settings.cache_clear()
    c = _client()
    res = c.post("/slack/events", json={"type": "url_verification", "challenge": "abc"})
    assert res.status_code == 200
    assert res.json()["challenge"] == "abc"


def test_logout_revokes_session(monkeypatch):
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    config.get_settings.cache_clear()
    c = _client()
    token = c.post("/auth/dev", json={}).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert c.get("/auth/me", headers=headers).status_code == 200
    assert c.post("/auth/logout", headers=headers).status_code == 200
    assert c.get("/auth/me", headers=headers).status_code == 401
