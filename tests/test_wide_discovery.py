"""Wide discovery: RSS, directory, aggregator (offline fixtures)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app import discovery, jobstore, profile, wide_discovery
from app.jobsources import aggregator, directory, rss
from app.jobsources import JobPosting

FIXTURES = Path(__file__).parent / "fixtures"


def test_rss_parse_fixture():
    xml = (FIXTURES / "rss_remoteok.xml").read_bytes()
    posts = rss._parse_xml(xml, "remoteok", "Remote OK")
    assert len(posts) == 2
    assert posts[0].source == "rss"
    assert "Acme" in posts[0].company
    assert "Backend" in posts[0].title
    assert posts[0].external_id.startswith("remoteok:")


def test_aggregator_parse_fixture():
    import json

    data = json.loads((FIXTURES / "aggregator_google_jobs.json").read_text())
    posts = aggregator._parse(data, "software engineer remote")
    assert len(posts) == 2
    assert posts[0].company == "Mystery Startup"
    assert posts[0].source == "aggregator"


def test_directory_batch_rotates(monkeypatch):
    monkeypatch.setattr(
        "app.jobsources.directory._load_boards",
        lambda: {"greenhouse": ["acme"], "lever": [], "ashby": []},
    )
    sample = [
        JobPosting("greenhouse", "99", "SWE", "https://x/1", company="Acme", description="python"),
    ]
    monkeypatch.setitem(
        directory._FETCHERS, "greenhouse", lambda t: sample if t == "acme" else []
    )
    jobstore.set_directory_cursor(0)
    batch = directory.fetch_directory_batch(boards_to_probe=1, max_jobs_per_board=5)
    assert len(batch) == 1
    assert batch[0].external_id == "greenhouse:acme:99"
    assert jobstore.get_directory_cursor() == 1


def test_collect_fresh_rss_only(monkeypatch):
    xml = (FIXTURES / "rss_remoteok.xml").read_bytes()
    monkeypatch.setattr("app.jobsources.rss._fetch_xml", lambda url: xml)
    monkeypatch.setenv("JOB_SOURCES_ENABLED", "rss")
    monkeypatch.setenv("JOB_WIDE_RSS_ENABLED", "true")
    monkeypatch.setenv("JOB_WIDE_RSS_FEEDS", "remoteok")
    monkeypatch.setenv("JOB_WIDE_DIRECTORY_ENABLED", "false")
    monkeypatch.setenv("JOB_WIDE_AGGREGATOR_ENABLED", "false")
    from app import config

    config.get_settings.cache_clear()

    profile.set_profile("u", roles="software engineer", keywords="python")
    prof = profile.get_profile("u")
    fresh = wide_discovery.collect_fresh("u", prof)
    assert len(fresh) >= 1
    assert all(p.source == "rss" for p in fresh)


def test_tick_runs_without_tracked_boards(monkeypatch):
    xml = (FIXTURES / "rss_remoteok.xml").read_bytes()
    monkeypatch.setattr("app.jobsources.rss._fetch_xml", lambda url: xml)
    monkeypatch.setenv("JOB_SOURCES_ENABLED", "rss")
    monkeypatch.setenv("JOB_WIDE_RSS_ENABLED", "true")
    monkeypatch.setenv("JOB_WIDE_RSS_FEEDS", "remoteok")
    monkeypatch.setenv("JOB_WIDE_DIRECTORY_ENABLED", "false")
    monkeypatch.setenv("JOB_WIDE_AGGREGATOR_ENABLED", "false")
    from app import config

    config.get_settings.cache_clear()

    profile.set_profile("u", roles="engineer", keywords="python")
    sent: list = []

    class Cap:
        def send(self, u, b):
            sent.append(b)

    n = discovery.tick("u", sender=Cap())
    assert n in (0, 1)
    assert jobstore.list_postings("u")


def test_profile_enables_wide_copy():
    from app.engine import handle_sms

    reply = handle_sms("u", "looking for new grad swe roles, remote")
    low = reply.lower()
    assert "wide discovery" in low or "feeds" in low or "job boards" in low
