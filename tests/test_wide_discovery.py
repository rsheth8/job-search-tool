"""Wide discovery: RSS, directory, aggregator, internship list (offline fixtures)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import discovery, jobstore, profile, wide_discovery
from app.jobsources import aggregator, directory, rss, swelist
from app.jobsources import JobPosting

FIXTURES = Path(__file__).parent / "fixtures"


class _FrozenDateTime(datetime):
    """Pin swelist age filter to the fixture snapshot date."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 8, 27, tzinfo=timezone.utc)


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


def test_aggregator_prefers_real_apply_link_over_share_link():
    data = {"jobs_results": [{
        "title": "Backend Engineer",
        "company_name": "Stripe",
        "job_id": "abc",
        "share_link": "https://www.google.com/search?q=...",
        "apply_options": [
            {"title": "Apply on Greenhouse", "link": "https://boards.greenhouse.io/stripe/jobs/1"},
            {"title": "LinkedIn", "link": "https://linkedin.com/jobs/1"},
        ],
    }]}
    posts = aggregator._parse(data, "backend")
    assert posts[0].url == "https://boards.greenhouse.io/stripe/jobs/1"  # not the google link


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


def test_swelist_parse_keeps_fresh_direct_ats_urls():
    import json

    data = json.loads((FIXTURES / "swelist_listings.json").read_text())
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    posts = swelist._parse(data, list_id="summer2027", max_age_days=21, now=now)
    ids = {p.external_id for p in posts}
    assert "summer2027:fresh-lever-1" in ids
    assert "summer2027:fresh-greenhouse-1" in ids
    assert "summer2027:proxy-internshiplist" in ids
    assert "summer2027:inactive-old" not in ids
    assert "summer2027:hidden-row" not in ids
    assert "summer2027:stale-but-active" not in ids
    assert "summer2027:no-url" not in ids
    lever = next(p for p in posts if p.company == "Voltus")
    assert lever.url.startswith("https://jobs.lever.co/voltus/")
    assert lever.source == "swelist"
    assert "Remote" in lever.location
    assert swelist.is_proxy_apply_url(lever.url) is False
    proxy = next(p for p in posts if p.company == "Geotab")
    assert swelist.is_proxy_apply_url(proxy.url) is True
    assert "proxy" in proxy.description.lower()
    assert [p.company for p in posts] == ["Voltus", "Astranis", "Geotab"]


def test_swelist_unwrap_replaces_proxy_url(monkeypatch):
    monkeypatch.setenv("JOB_SWELIST_MAX_AGE_DAYS", "21")
    from app import config

    config.get_settings.cache_clear()
    monkeypatch.setattr("app.jobsources.swelist.datetime", _FrozenDateTime)
    listing = [{
        "id": "proxy-1",
        "company_name": "Geotab",
        "title": "Intern",
        "url": "https://job-boards.greenhouse.io/internshiplist2000/jobs/1",
        "locations": ["Toronto"],
        "active": True,
        "is_visible": True,
        "date_posted": 1787529600,
        "date_updated": 1787529600,
    }]
    monkeypatch.setattr("app.jobsources.swelist.get_json", lambda url: listing)
    monkeypatch.setattr(
        "app.jobsources.swelist._unwrap_apply_url",
        lambda url: "https://job-boards.greenhouse.io/geotab/jobs/99",
    )
    posts = swelist.fetch("summer2027")
    assert len(posts) == 1
    assert posts[0].url == "https://job-boards.greenhouse.io/geotab/jobs/99"


def test_collect_fresh_swelist(monkeypatch):
    import json

    data = json.loads((FIXTURES / "swelist_listings.json").read_text())
    monkeypatch.setattr("app.jobsources.swelist.get_json", lambda url: data)
    monkeypatch.setattr(
        "app.jobsources.swelist._unwrap_apply_url", lambda url: url
    )
    monkeypatch.setenv("JOB_SOURCES_ENABLED", "swelist")
    monkeypatch.setenv("JOB_WIDE_SWELIST_ENABLED", "true")
    monkeypatch.setenv("JOB_WIDE_RSS_ENABLED", "false")
    monkeypatch.setenv("JOB_WIDE_DIRECTORY_ENABLED", "false")
    monkeypatch.setenv("JOB_WIDE_AGGREGATOR_ENABLED", "false")
    monkeypatch.setenv("JOB_SWELIST_MAX_AGE_DAYS", "21")
    from app import config

    config.get_settings.cache_clear()
    monkeypatch.setattr("app.jobsources.swelist.datetime", _FrozenDateTime)

    profile.set_profile("u", roles="software intern", keywords="python")
    prof = profile.get_profile("u")
    fresh = wide_discovery.collect_fresh("u", prof)
    assert len(fresh) == 3
    assert all(p.source == "swelist" for p in fresh)
    assert any("lever.co/voltus" in p.url for p in fresh)


def test_profile_enables_wide_copy():
    from app.engine import handle_sms

    reply = handle_sms("u", "looking for new grad swe roles, remote")
    low = reply.lower()
    assert "wide discovery" in low or "feeds" in low or "job boards" in low
