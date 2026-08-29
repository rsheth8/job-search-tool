"""Apply flow mentions tailored resume in the chat reply."""
from __future__ import annotations

from app import jobstore
from app.engine import handle_sms
from app.jobsources.base import JobPosting


def test_apply_job_mentions_tailored_resume(monkeypatch, tmp_path):
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
    assert "Tailored resume" in reply or "Reusing saved resume" in reply
    assert "Open Apply" in reply
