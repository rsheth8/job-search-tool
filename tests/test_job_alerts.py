"""Digest + review formatting."""
from __future__ import annotations

from app import job_alerts
from app.jobsources import JobPosting


def test_build_digest_summarizes_batch():
    posts = [
        (JobPosting("greenhouse", "1", "SWE", "https://x/1", company="Stripe"), 0.9, 10),
        (JobPosting("greenhouse", "2", "PM", "https://x/2", company="Stripe"), 0.7, 11),
        (JobPosting("lever", "3", "Backend", "https://x/3", company="Ramp"), 0.65, 12),
    ]
    body = job_alerts.build_digest(posts)
    assert "3 new job matches" in body
    assert "Stripe 2" in body
    assert "review jobs" in body
    assert "#10" in body


def test_build_review_card_shows_position():
    p = JobPosting("greenhouse", "1", "Engineer", "https://x/1", company="Acme", location="NYC")
    body = job_alerts.build_review_card(p, 0.82, 7, position=2, total=5)
    assert "2 of 5" in body
    assert "#7" in body
    assert "skip" in body.lower()
