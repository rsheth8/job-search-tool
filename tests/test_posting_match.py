"""Posting ↔ application matching and duplicate-alert suppression."""
from __future__ import annotations

from app import discovery, jobstore, store
from app.engine import handle_sms
from app.jobsources import JobPosting
from app.posting_match import (
    matches_application,
    normalize_external_id,
    titles_similar,
    user_already_applied_to,
)


def test_normalize_external_id_namespaces():
    assert normalize_external_id("greenhouse", "stripe", "123") == "greenhouse:stripe:123"
    assert normalize_external_id("greenhouse", "stripe", "greenhouse:stripe:123") == (
        "greenhouse:stripe:123"
    )


def test_titles_similar_swe_and_software_engineer():
    assert titles_similar("SWE", "Software Engineer")
    assert titles_similar("Backend Engineer", "Senior Backend Engineer")
    assert not titles_similar("Product Manager", "Software Engineer")


def test_matches_application_company_and_role():
    assert matches_application("Stripe", "SWE", "Stripe", "Software Engineer")
    assert not matches_application("Stripe", "SWE", "Notion", "Software Engineer")


def test_manual_apply_marks_matching_posting():
    jobstore.save_posting(
        "u",
        JobPosting("greenhouse", "1", "Software Engineer", "https://x/1", company="Stripe"),
        relevance_score=0.9,
        status="queued",
    )
    handle_sms("u", "applied stripe swe")
    assert jobstore.get_posting("u", 1)["status"] == "applied"
    assert user_already_applied_to("u", "Stripe", "Software Engineer")


def test_any_new_jobs_hides_already_applied_role():
    jobstore.add_tracked_company("u", "greenhouse", "stripe", "Stripe")
    jobstore.save_posting(
        "u",
        JobPosting("greenhouse", "1", "Software Engineer", "https://x/1", company="Stripe"),
        relevance_score=0.9,
        status="queued",
    )
    store.create_application("u", "Stripe", "Software Engineer")
    reply = handle_sms("u", "any new jobs")
    assert "Software Engineer" not in reply
    assert "no jobs" in reply.lower()


def test_discovery_skips_alert_when_already_applied(monkeypatch):
    jobstore.add_tracked_company("u", "greenhouse", "acme", "Acme")
    from app import profile

    profile.set_profile("u", roles="software engineer", keywords="python")
    store.create_application("u", "Acme", "Software Engineer")

    feed = [
        JobPosting("greenhouse", "1", "Software Engineer", "https://x/1",
                   company="Acme", description="python kubernetes"),
    ]
    monkeypatch.setattr("app.discovery.fetch_source", lambda s, t: feed if t == "acme" else [])

    sent: list = []

    class Cap:
        def send(self, u, b):
            sent.append(b)

    assert discovery.tick("u", sender=Cap()) == 0
    row = jobstore.list_postings("u")[0]
    assert row["status"] == "applied"
    assert not sent


def test_tracked_and_directory_share_external_id(monkeypatch):
    """Same ATS job id via tracked board vs directory dedupes to one row."""
    from app import profile

    profile.set_profile("u", roles="software engineer", keywords="python")
    jobstore.add_tracked_company("u", "greenhouse", "acme", "Acme")

    feed = [
        JobPosting("greenhouse", "99", "Software Engineer", "https://x/99",
                   company="Acme", description="python"),
    ]
    monkeypatch.setattr("app.discovery.fetch_source", lambda s, t: feed if t == "acme" else [])

    directory_batch: list[JobPosting] = []
    monkeypatch.setattr(
        "app.wide_discovery.collect_fresh",
        lambda user_id, prof, existing_keys=None: list(directory_batch),
    )

    class Cap:
        def send(self, u, b):
            pass

    discovery.tick("u", sender=Cap())
    assert jobstore.posting_exists("u", "greenhouse", "greenhouse:acme:99")
    assert len(jobstore.list_postings("u")) == 1

    # Directory would surface the same role with the same namespaced external id.
    directory_batch.append(
        JobPosting(
            "greenhouse", "greenhouse:acme:99", "Software Engineer", "https://x/99",
            company="Acme", description="python",
        )
    )
    assert discovery.tick("u", sender=Cap()) == 0
    assert len(jobstore.list_postings("u")) == 1
