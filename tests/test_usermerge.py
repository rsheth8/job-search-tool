"""Merging a user's data from one id into another (account consolidation)."""
from __future__ import annotations

from app import applicant, jobstore, profile, reranker, store, usermerge
from app.jobsources import JobPosting


def _posting(ext, title="Software Engineer", source="greenhouse"):
    return JobPosting(source=source, external_id=ext, title=title, url="https://x",
                      company="Acme", location="Remote", description="Build software.")


def _seed(uid):
    profile.set_profile(uid, roles="software engineer")
    applicant.set_identity(uid, {"email": f"{uid}@x.com"})
    store.create_application(uid, "Stripe", "Backend Engineer", status="Applied")
    jobstore.save_posting(uid, _posting("1"), relevance_score=0.7, status="queued")


def test_merge_moves_all_user_scoped_rows():
    _seed("local")
    moved = usermerge.merge_user("local", "U123")

    # Everything seeded moved to the new id.
    assert "applications" in moved and "job_search_profile" in moved
    assert "job_postings" in moved and "applications" in moved
    assert profile.get_profile("U123")["roles"] == "software engineer"
    assert applicant.get_identity("U123")["email"] == "local@x.com"
    assert len(store.list_applications("U123")) == 1
    assert len(jobstore.list_postings("U123", statuses=("queued",))) == 1
    # Source is now empty of those rows.
    assert profile.get_profile("local") is None
    assert store.list_applications("local") == []


def test_merge_repoints_trained_model_and_labels():
    # Build a trained re-ranker under 'local'.
    for i in range(6):
        jobstore.save_posting("local", _posting(f"p{i}"), relevance_score=0.6, status="applied")
    for i in range(6):
        jobstore.save_posting("local", _posting(f"n{i}", source="rss"),
                              relevance_score=0.3, status="dismissed")
    profile.set_profile("local", roles="software engineer")
    assert reranker.train("local", profile.get_profile("local")) is not None

    usermerge.merge_user("local", "U123")
    assert reranker.load_model("U123") is not None       # model followed
    assert reranker.load_model("local") is None


def test_dry_run_reports_without_moving():
    _seed("local")
    preview = usermerge.merge_user("local", "U123", dry_run=True)
    assert preview["applications"] == 1
    # Nothing actually moved.
    assert profile.get_profile("local") is not None
    assert profile.get_profile("U123") is None


def test_merge_skips_rows_that_would_collide():
    # Both ids already have a profile (single-row-per-user). The merge must keep
    # the destination's row rather than error or overwrite.
    profile.set_profile("local", roles="from local")
    profile.set_profile("U123", roles="from dest")
    usermerge.merge_user("local", "U123")
    assert profile.get_profile("U123")["roles"] == "from dest"   # dst preserved


def test_same_id_is_noop():
    _seed("local")
    assert usermerge.merge_user("local", "local") == {}
