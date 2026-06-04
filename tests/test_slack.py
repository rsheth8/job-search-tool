"""Slack transport: signature checks, the /slack/events webhook, and dispatch.

Network is never touched — ``post_message`` is monkeypatched everywhere. The
heuristic router (forced by conftest) handles ``handle_sms`` offline.
"""
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import slack


@pytest.fixture(autouse=True)
def _clear_seen():
    slack._seen_event_ids.clear()
    yield
    slack._seen_event_ids.clear()


def _sign(secret: str, body: bytes, ts: str | None = None) -> dict:
    ts = ts or str(int(time.time()))
    base = b"v0:" + ts.encode() + b":" + body
    digest = hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": f"v0={digest}"}


# --- signature verification ------------------------------------------------

def test_verify_signature_roundtrip():
    secret, body = "shh", b'{"hello":"world"}'
    h = _sign(secret, body)
    assert slack.verify_signature(
        secret, h["X-Slack-Request-Timestamp"], body, h["X-Slack-Signature"]
    )


def test_verify_signature_rejects_tampered_body():
    secret, body = "shh", b'{"hello":"world"}'
    h = _sign(secret, body)
    assert not slack.verify_signature(
        secret, h["X-Slack-Request-Timestamp"], b'{"hello":"evil"}', h["X-Slack-Signature"]
    )


def test_verify_signature_rejects_stale_timestamp():
    secret, body = "shh", b"{}"
    old = str(int(time.time()) - 10_000)
    h = _sign(secret, body, ts=old)
    assert not slack.verify_signature(secret, old, body, h["X-Slack-Signature"])


def test_verify_signature_rejects_missing_parts():
    assert not slack.verify_signature("shh", "", b"{}", "")


# --- the webhook -----------------------------------------------------------

def _client():
    from app.main import app

    return TestClient(app)


def test_url_verification_echoes_challenge():
    resp = _client().post(
        "/slack/events", json={"type": "url_verification", "challenge": "abc123"}
    )
    assert resp.status_code == 200
    assert resp.json()["challenge"] == "abc123"


def test_event_callback_dispatches_to_handler(monkeypatch):
    seen = {}
    monkeypatch.setattr(slack, "handle_event", lambda payload: seen.update(payload))
    payload = {
        "type": "event_callback",
        "event": {"type": "message", "text": "hi", "user": "U1", "channel": "D1"},
    }
    resp = _client().post("/slack/events", json=payload)
    assert resp.status_code == 200
    assert seen.get("type") == "event_callback"  # background task ran


def test_bad_signature_is_rejected(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shh")
    from app import config

    config.get_settings.cache_clear()
    resp = _client().post(
        "/slack/events",
        content=b"{}",
        headers={"X-Slack-Request-Timestamp": "1", "X-Slack-Signature": "v0=nope"},
    )
    assert resp.status_code == 403


def test_good_signature_passes(monkeypatch):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "shh")
    from app import config

    config.get_settings.cache_clear()
    monkeypatch.setattr(slack, "handle_event", lambda payload: None)
    body = json.dumps({"type": "event_callback", "event": {}}).encode()
    resp = _client().post("/slack/events", content=body, headers=_sign("shh", body))
    assert resp.status_code == 200


# --- handle_event ----------------------------------------------------------

def _msg(text="applied stripe swe", user="U1", channel="D1", **extra):
    event = {"type": "message", "text": text, "user": user, "channel": channel}
    event.update(extra)
    return {"type": "event_callback", "event": event, "event_id": "Ev1"}


def test_handle_event_runs_brain_and_replies(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    from app import config

    config.get_settings.cache_clear()
    posts = []
    monkeypatch.setattr(slack, "post_message", lambda token, ch, text: posts.append((ch, text)))

    slack.handle_event(_msg())

    assert len(posts) == 1
    channel, reply = posts[0]
    assert channel == "D1"
    assert reply  # the engine produced something
    # and the application was actually logged
    from app import store

    assert store.find_application("U1", "stripe") is not None


def test_handle_event_dedupes_redelivery(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    from app import config

    config.get_settings.cache_clear()
    posts = []
    monkeypatch.setattr(slack, "post_message", lambda token, ch, text: posts.append(text))

    payload = _msg(text="hello")
    slack.handle_event(payload)
    slack.handle_event(payload)  # same event_id → ignored
    assert len(posts) == 1


def test_handle_event_ignores_bot_messages(monkeypatch):
    posts = []
    monkeypatch.setattr(slack, "post_message", lambda *a: posts.append(a))
    slack.handle_event(_msg(bot_id="B1"))
    slack.handle_event(_msg(subtype="message_changed"))
    assert posts == []


def test_handle_event_strips_leading_mention(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    from app import config

    config.get_settings.cache_clear()
    monkeypatch.setattr(slack, "post_message", lambda *a: None)
    captured = {}

    def fake_handle_sms(user, text):
        captured["text"] = text
        return "ok"

    monkeypatch.setattr("app.engine.handle_sms", fake_handle_sms)
    slack.handle_event(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "text": "<@U0BOT> applied notion",
                "user": "U1",
                "channel": "C1",
            },
            "event_id": "Ev2",
        }
    )
    assert captured["text"] == "applied notion"


# --- SlackSender (outbound reminders) --------------------------------------

def test_slack_sender_posts_dm(monkeypatch):
    posts = []
    monkeypatch.setattr(slack, "post_message", lambda token, ch, text: posts.append((token, ch, text)))
    slack.SlackSender("xoxb-x").send("U9", "⏰ follow up?")
    assert posts == [("xoxb-x", "U9", "⏰ follow up?")]


def test_get_sender_prefers_slack(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    from app import config, reminders

    config.get_settings.cache_clear()
    reminders._sender_singleton = None
    assert isinstance(reminders.get_sender(), slack.SlackSender)
