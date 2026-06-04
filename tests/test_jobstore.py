"""Tracked-board + posting persistence (uses the autouse temp_db fixture)."""
from __future__ import annotations

from app import jobstore
from app.jobsources import JobPosting


def _posting(ext="1", title="SWE", score=None):
    return JobPosting(
        source="greenhouse", external_id=ext, title=title,
        url=f"https://x/{ext}", company="Acme", location="NYC", description="desc",
    )


def test_track_dedupes_and_lists():
    assert jobstore.add_tracked_company("u", "greenhouse", "acme", "Acme") is not None
    # Same (user, source, token) is a no-op.
    assert jobstore.add_tracked_company("u", "greenhouse", "acme", "Acme") is None
    rows = jobstore.list_tracked("u")
    assert len(rows) == 1 and rows[0]["company_name"] == "Acme"


def test_save_posting_dedupes_on_source_and_external_id():
    first = jobstore.save_posting("u", _posting("42"), relevance_score=0.9, status="alerted")
    assert first is not None
    assert jobstore.posting_exists("u", "greenhouse", "42")
    # Second save of the same posting is ignored (scored/alerted once, ever).
    assert jobstore.save_posting("u", _posting("42")) is None
    assert len(jobstore.list_postings("u")) == 1


def test_list_postings_orders_by_relevance_and_filters_status():
    jobstore.save_posting("u", _posting("1"), relevance_score=0.3, status="new")
    jobstore.save_posting("u", _posting("2"), relevance_score=0.95, status="alerted")
    jobstore.save_posting("u", _posting("3"), relevance_score=None, status="new")
    ordered = jobstore.list_postings("u")
    assert ordered[0]["external_id"] == "2"  # highest score first
    assert ordered[-1]["external_id"] == "3"  # unscored sinks last
    alerted = jobstore.list_postings("u", statuses=("alerted",))
    assert [r["external_id"] for r in alerted] == ["2"]


def test_mark_status_and_counts():
    row = jobstore.save_posting("u", _posting("7"), status="new")
    jobstore.mark_posting_status(row["id"], "applied")
    assert jobstore.get_posting("u", row["id"])["status"] == "applied"
    assert jobstore.counts_by_status("u") == {"applied": 1}
