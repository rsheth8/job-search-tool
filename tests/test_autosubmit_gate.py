"""Auto-submit is off for testers unless explicitly enabled."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import apply_queue, config, jobstore
from app.jobsources import JobPosting
from app.main import app


def test_autosubmit_disabled_returns_403(monkeypatch):
    monkeypatch.setenv("APPLY_AUTOSUBMIT_ENABLED", "false")
    config.get_settings.cache_clear()
    posting = jobstore.save_posting(
        "u1",
        JobPosting(
            source="greenhouse", external_id="1", title="SWE",
            url="https://boards.greenhouse.io/acme/jobs/1", company="Acme",
            location="Remote", description="Python",
        ),
        relevance_score=0.9, status="queued",
    )
    apply_queue.stage("u1", posting["id"])
    r = TestClient(app).post(
        "/apply/autosubmit", json={"user": "u1", "posting_id": posting["id"]}
    )
    assert r.status_code == 403
