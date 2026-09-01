"""Enabled is not the same as working.

Every adapter in this app fails open: a wrong tenant, a changed JSON shape, a
403 from bot protection all return ``[]``, log at warning, and let the tick
carry on. That is the right behaviour at 3am and it means a source can be
switched on, deployed, and contributing nothing while /health says ok — which
is precisely what happened to four sources this week for a different reason.

So /health counts what each source has actually produced. An absence becomes a
number.
"""
from __future__ import annotations

import pytest

from app import jobstore
from app.jobsources import JobPosting


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _save(user, source, ext, **kw):
    return jobstore.save_posting(
        user,
        JobPosting(source=source, external_id=str(ext),
                   title=kw.get("title", "Software Engineer"),
                   url=f"https://x/{source}/{ext}",
                   company=kw.get("company", "Acme"),
                   location="Remote", description="Build software."),
        relevance_score=0.7, status="queued",
    )


def test_a_source_that_produced_nothing_is_counted_as_nothing():
    assert jobstore.source_freshness(7) == {}


def test_it_counts_what_each_source_produced():
    _save("u1", "workday", 1)
    _save("u1", "workday", 2)
    _save("u1", "amazon", 3)
    assert jobstore.source_freshness(7) == {"workday": 2, "amazon": 1}


def test_it_counts_across_users_not_per_queue():
    """The question is whether the adapter works, not whose list it landed in."""
    _save("u1", "netflix", 1)
    _save("u2", "netflix", 2)
    assert jobstore.source_freshness(7)["netflix"] == 2


def test_an_old_posting_falls_out_of_the_window():
    row = _save("u1", "usajobs", 1)
    from app.db import connect
    with connect() as conn:
        conn.execute(
            "UPDATE job_postings SET first_seen_at = '2020-01-01T00:00:00+00:00' "
            "WHERE id = ?", (row["id"],))
    assert "usajobs" not in jobstore.source_freshness(7)
    assert jobstore.source_freshness(3650)["usajobs"] == 1


def test_the_window_is_never_zero_days():
    _save("u1", "greenhouse", 1)
    assert jobstore.source_freshness(0) == {"greenhouse": 1}


# --- /health --------------------------------------------------------------

def test_health_names_the_sources_that_have_produced_nothing(client, monkeypatch):
    monkeypatch.setenv("JOB_SOURCES_ENABLED", "greenhouse,workday,amazon")
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        _save("u1", "workday", 1)
        disc = client.get("/health").json()["discovery"]
        assert disc["postings_by_source_7d"] == {"workday": 1}
        assert disc["silent_sources"] == ["greenhouse", "amazon"]
    finally:
        get_settings.cache_clear()


def test_the_newest_sources_are_not_exempt_from_the_check(client, monkeypatch):
    """They're all in NON_BOARD_SOURCES, which means "the token isn't a company
    slug" and says nothing about whether the adapter works. Filtering on it
    would have excluded the four sources this was built to watch."""
    monkeypatch.setenv("JOB_SOURCES_ENABLED", "workday,amazon,netflix,usajobs")
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        disc = client.get("/health").json()["discovery"]
        assert set(disc["silent_sources"]) == {
            "workday", "amazon", "netflix", "usajobs"}
    finally:
        get_settings.cache_clear()
