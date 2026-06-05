"""Slack PDF upload: mocked API flow + optional live smoke test."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app import slack

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PDF = REPO_ROOT / "resumes" / "Resume_SWE (7).pdf"


class _MockTransport(httpx.BaseTransport):
    """Simulate Slack's 3-step external upload flow."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:  # noqa: D401
        url = str(request.url)
        self.calls.append((request.method, url))

        if url.endswith("/files.getUploadURLExternal"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "upload_url": "https://files.example/upload",
                    "file_id": "F123",
                },
            )

        if url == "https://files.example/upload":
            assert request.content.startswith(b"%PDF") or request.content
            return httpx.Response(200, text="OK")

        if url.endswith("/files.completeUploadExternal"):
            body = json.loads(request.content.decode())
            assert body["files"] == [{"id": "F123", "title": "Resume_test.pdf"}]
            assert body.get("channel_id") == "C999" or body.get("channels") == "C999"
            return httpx.Response(
                200,
                json={"ok": True, "files": [{"id": "F123", "title": "Resume_test.pdf"}]},
            )

        if url.endswith("/chat.postMessage"):
            return httpx.Response(200, json={"ok": True})

        return httpx.Response(404, json={"ok": False, "error": "unexpected_url"})


def test_upload_file_external_flow(monkeypatch):
    transport = _MockTransport()
    client = httpx.Client(transport=transport)

    def fake_client(*args, **kwargs):
        return client

    monkeypatch.setattr("httpx.post", lambda *a, **k: client.post(*a, **k))

    ok = slack.upload_file(
        "xoxb-test",
        "C999",
        "Resume_test.pdf",
        b"%PDF-1.4 sample",
        comment="Tailored resume",
    )
    assert ok is True
    urls = [u for _, u in transport.calls]
    assert any(u.endswith("files.getUploadURLExternal") for u in urls)
    assert "https://files.example/upload" in urls
    assert any(u.endswith("files.completeUploadExternal") for u in urls)


def test_upload_file_uses_channels_for_user_id(monkeypatch):
    captured: dict = {}

    def fake_post(url, **kwargs):
        url_s = str(url)
        if url_s.endswith("files.getUploadURLExternal"):
            return httpx.Response(
                200,
                json={"ok": True, "upload_url": "https://files.example/upload", "file_id": "F1"},
            )
        if url_s == "https://files.example/upload":
            return httpx.Response(200, text="OK")
        if url_s.endswith("files.completeUploadExternal"):
            captured["json"] = kwargs.get("json")
            return httpx.Response(200, json={"ok": True, "files": []})
        raise AssertionError(url_s)

    monkeypatch.setattr("httpx.post", fake_post)
    assert slack.upload_file("xoxb", "U0ABCDEFGH", "r.pdf", b"%PDF") is True
    assert captured["json"]["channels"] == "U0ABCDEFGH"
    assert "channel_id" not in captured["json"]


def test_upload_file_reports_api_error(monkeypatch):
    def fake_post(url, **kwargs):
        if str(url).endswith("files.getUploadURLExternal"):
            return httpx.Response(200, json={"ok": False, "error": "missing_scope"})
        raise AssertionError(f"unexpected call: {url}")

    monkeypatch.setattr("httpx.post", fake_post)
    assert slack.upload_file("xoxb", "C1", "x.pdf", b"%PDF") is False


def test_post_reply_with_attachments_posts_text_and_pdf(monkeypatch):
    messages: list[str] = []
    uploads: list[tuple] = []

    monkeypatch.setattr(
        slack, "post_message", lambda token, ch, text: messages.append(text) or True
    )
    monkeypatch.setattr(
        slack,
        "upload_file",
        lambda token, ch, name, data, **k: uploads.append((name, data)) or True,
    )

    from app.engine import _queue_attachment

    _queue_attachment("U1", "Resume_Acme.pdf", b"%PDF-bytes")
    ok = slack.post_reply_with_attachments(
        "xoxb", "D123", "U1", "Apply link + draft here"
    )
    assert ok is True
    assert messages == ["Apply link + draft here"]
    assert uploads == [("Resume_Acme.pdf", b"%PDF-bytes")]


def test_handle_event_uploads_after_apply(monkeypatch):
    posts: list[tuple] = []

    monkeypatch.setattr(
        slack,
        "post_reply_with_attachments",
        lambda token, ch, uid, reply: posts.append((token, ch, uid, reply)) or True,
    )

    def fake_handle(user, text):
        from app.engine import _queue_attachment

        _queue_attachment(user, "Resume_test.pdf", b"%PDF")
        return "draft + link"

    monkeypatch.setattr("app.engine.handle_sms", fake_handle)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-live")
    from app import config

    config.get_settings.cache_clear()

    slack.handle_event({
        "type": "event_callback",
        "event_id": "Ev-upload-1",
        "event": {
            "type": "message",
            "text": "apply 1",
            "user": "U1",
            "channel": "D1",
        },
    })

    assert posts == [("xoxb-live", "D1", "U1", "draft + link")]


@pytest.mark.live_slack
def test_live_slack_pdf_upload():
    """Send a real PDF to Slack — requires SLACK_BOT_TOKEN + SLACK_TEST_CHANNEL.

    Run: pytest tests/test_slack_upload.py -m live_slack -v
    """
    import os

    from dotenv import dotenv_values

    env = dotenv_values(REPO_ROOT / ".env")
    token = (os.environ.get("SLACK_BOT_TOKEN") or env.get("SLACK_BOT_TOKEN") or "").strip()
    channel = (
        os.environ.get("SLACK_TEST_CHANNEL")
        or env.get("SLACK_TEST_CHANNEL")
        or ""
    ).strip()
    user = (
        os.environ.get("SLACK_TEST_USER")
        or env.get("SLACK_TEST_USER")
        or env.get("JOB_ALERT_USER")
        or ""
    ).strip()

    if not token:
        pytest.skip("SLACK_BOT_TOKEN not set")
    if not channel and not user:
        pytest.skip("Set SLACK_TEST_CHANNEL (D...) or SLACK_TEST_USER (U...)")

    if not channel and user:
        channel = slack.open_dm_channel(token, user)
        assert channel, "conversations.open failed — add im:write scope?"

    assert SAMPLE_PDF.is_file(), f"missing sample PDF: {SAMPLE_PDF}"
    pdf_bytes = SAMPLE_PDF.read_bytes()

    assert slack.post_message(
        token,
        channel,
        "🧪 Resume upload smoke test — you should see a PDF attached below.",
    )
    ok = slack.upload_file(
        token,
        channel,
        "Resume_SWE_smoke_test.pdf",
        pdf_bytes,
        comment="Smoke test: tailored resume attachment",
    )
    assert ok is True
