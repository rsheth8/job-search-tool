"""Wide discovery: RSS, directory, internship list (offline fixtures)."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app import discovery, jobstore, profile, wide_discovery
from app.jobsources import directory, rss, swelist
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
    from app import config

    config.get_settings.cache_clear()

    profile.set_profile("u", roles="software engineer", keywords="python")
    prof = profile.get_profile("u")
    fresh = wide_discovery.collect_fresh("u", prof)
    assert len(fresh) >= 1
    assert all(p.source == "rss" for p in fresh)


def test_marketing_profile_adds_wwr_sales_not_programming(monkeypatch):
    urls: list[str] = []

    def capture(url):
        urls.append(url)
        return b"<rss version='2.0'><channel></channel></rss>"

    monkeypatch.setattr("app.jobsources.rss._fetch_xml", capture)
    monkeypatch.setenv("JOB_SOURCES_ENABLED", "rss")
    monkeypatch.setenv("JOB_WIDE_RSS_ENABLED", "true")
    monkeypatch.setenv("JOB_WIDE_RSS_FEEDS", "remoteok,weworkremotely")
    monkeypatch.setenv("JOB_WIDE_DIRECTORY_ENABLED", "false")
    from app import config

    config.get_settings.cache_clear()
    profile.set_profile("u", roles="marketing coordinator", keywords="brand")
    prof = profile.get_profile("u")
    ids = wide_discovery.wide_rss_feed_ids(prof)
    assert "remoteok" in ids
    assert "wwr-sales" in ids
    assert "weworkremotely" not in ids
    wide_discovery.collect_fresh("u", prof)
    assert any("remoteok" in u for u in urls)
    assert any("sales-and-marketing" in u for u in urls)
    assert not any("programming" in u for u in urls)


def test_swe_profile_keeps_programming_wwr(monkeypatch):
    monkeypatch.setenv("JOB_SOURCES_ENABLED", "rss")
    monkeypatch.setenv("JOB_WIDE_RSS_ENABLED", "true")
    monkeypatch.setenv("JOB_WIDE_RSS_FEEDS", "remoteok,weworkremotely")
    monkeypatch.setenv("JOB_WIDE_DIRECTORY_ENABLED", "false")
    from app import config

    config.get_settings.cache_clear()
    profile.set_profile("u", roles="software engineer", keywords="python")
    ids = wide_discovery.wide_rss_feed_ids(profile.get_profile("u"))
    assert "remoteok" in ids
    assert "weworkremotely" in ids
    assert "wwr-sales" not in ids


def test_tick_runs_without_tracked_boards(monkeypatch):
    xml = (FIXTURES / "rss_remoteok.xml").read_bytes()
    monkeypatch.setattr("app.jobsources.rss._fetch_xml", lambda url: xml)
    monkeypatch.setenv("JOB_SOURCES_ENABLED", "rss")
    monkeypatch.setenv("JOB_WIDE_RSS_ENABLED", "true")
    monkeypatch.setenv("JOB_WIDE_RSS_FEEDS", "remoteok")
    monkeypatch.setenv("JOB_WIDE_DIRECTORY_ENABLED", "false")
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
    monkeypatch.setattr("app.jobsources.swelist.get_json", lambda url, **k: listing)
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
    monkeypatch.setattr("app.jobsources.swelist.get_json", lambda url, **k: data)
    monkeypatch.setattr(
        "app.jobsources.swelist._unwrap_apply_url", lambda url: url
    )
    monkeypatch.setenv("JOB_SOURCES_ENABLED", "swelist")
    monkeypatch.setenv("JOB_WIDE_SWELIST_ENABLED", "true")
    monkeypatch.setenv("JOB_WIDE_RSS_ENABLED", "false")
    monkeypatch.setenv("JOB_WIDE_DIRECTORY_ENABLED", "false")
    monkeypatch.setenv("JOB_SWELIST_LIST", "summer2027")
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


def test_directory_learns_slugs_from_apply_urls():
    posts = [
        JobPosting(
            "swelist", "1", "Intern",
            "https://jobs.lever.co/voltus/abc", company="Voltus",
        ),
        JobPosting(
            "rss", "2", "SWE",
            "https://job-boards.greenhouse.io/internshiplist2000/jobs/1",
            company="Proxy",
        ),
    ]
    n = directory.learn_from_postings(posts)
    assert n == 1
    assert ("lever", "voltus") in jobstore.list_learned_boards()


def test_collect_fresh_yc(monkeypatch):
    html = (FIXTURES / "yc_jobs.html").read_text()
    monkeypatch.setattr("app.jobsources.yc.get_text", lambda url, **k: html)
    monkeypatch.setenv("JOB_SOURCES_ENABLED", "yc")
    monkeypatch.setenv("JOB_WIDE_YC_ENABLED", "true")
    monkeypatch.setenv("JOB_WIDE_RSS_ENABLED", "false")
    monkeypatch.setenv("JOB_WIDE_DIRECTORY_ENABLED", "false")
    monkeypatch.setenv("JOB_WIDE_SWELIST_ENABLED", "false")
    from app import config

    config.get_settings.cache_clear()
    profile.set_profile("u", roles="software engineer", keywords="python")
    prof = profile.get_profile("u")
    fresh = wide_discovery.collect_fresh("u", prof)
    assert len(fresh) == 2
    assert all(p.source == "yc" for p in fresh)
    assert any(p.company == "Accord" for p in fresh)


def test_profile_enables_wide_copy():
    from app.engine import handle_sms

    reply = handle_sms("u", "looking for new grad swe roles, remote")
    low = reply.lower()
    assert "wide discovery" in low or "feeds" in low or "job boards" in low
