"""Apply flow attaches tailored resume PDFs to Slack replies."""
from __future__ import annotations

from app import jobstore, slack
from app.engine import consume_attachments, handle_sms
from app.jobsources.base import JobPosting


def test_apply_job_queues_resume_attachment(monkeypatch, tmp_path):
    from app import config

    minimal = r"""
\documentclass{article}
\begin{document}
One page resume content for testing.
\end{document}
"""
    (tmp_path / "swe.tex").write_text(minimal)
    (tmp_path / "aiml.tex").write_text(minimal)
    monkeypatch.setenv("RESUME_TEX_DIR", str(tmp_path))
    monkeypatch.setenv("RESUME_TAILOR_ENABLED", "true")
    config.get_settings.cache_clear()

    import shutil

    if not shutil.which("tectonic"):
        monkeypatch.setattr(
            "app.resume_tailor.tailor_for_posting",
            lambda user_id, *a, **k: type("R", (), {
                "pdf_bytes": b"%PDF-test",
                "filename": "Resume_test.pdf",
                "variant": "swe",
                "pages": 1,
                "from_cache": False,
            })(),
        )

    row = jobstore.save_posting(
        "u",
        JobPosting("greenhouse", "1", "Backend Engineer", "https://x/1",
                   company="Acme", description="python apis"),
        relevance_score=0.82, status="alerted",
    )
    reply = handle_sms("u", f"apply {row['id']}")
    assert "Tailored resume attached" in reply or "Reusing saved resume" in reply
    files = consume_attachments("u")
    assert len(files) == 1
    assert files[0][0].endswith(".pdf")
    assert files[0][1][:4] in (b"%PDF", b"%PDF-test")


def test_slack_handle_event_uploads_attachments(monkeypatch):
    posts: list[tuple] = []

    monkeypatch.setattr(
        slack,
        "post_reply_with_attachments",
        lambda token, ch, uid, reply: posts.append((token, ch, uid, reply)) or True,
    )

    def fake_handle(user, text):
        from app.engine import _queue_attachment

        _queue_attachment(user, "Resume_test.pdf", b"%PDF-bytes")
        return "done"

    monkeypatch.setattr("app.engine.handle_sms", fake_handle)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    from app import config

    config.get_settings.cache_clear()

    slack.handle_event({
        "type": "event_callback",
        "event_id": "Ev1",
        "event": {
            "type": "message",
            "text": "apply 1",
            "user": "U1",
            "channel": "C1",
        },
    })

    assert posts
    assert posts[0][2] == "U1"
    assert posts[0][3] == "done"
