"""Adapter parsing tests — pure, offline, fixture JSON (no network)."""
from __future__ import annotations

from pathlib import Path

from app.jobsources import (
    ashby, fetch_source, greenhouse, lever, rss, smartrecruiters, swelist,
    workable, yc,
)


def test_greenhouse_parse_normalizes_fields_and_strips_html():
    data = {
        "jobs": [
            {
                "id": 12345,
                "title": "Software Engineer, Backend",
                "location": {"name": "New York, NY"},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/12345",
                "updated_at": "2026-06-01T10:00:00-04:00",
                "first_published": "2026-01-15T09:00:00-04:00",
                "content": "&lt;p&gt;Build &lt;b&gt;things&lt;/b&gt;.&lt;/p&gt;",
            }
        ]
    }
    posts = greenhouse._parse(data, "acme")
    assert len(posts) == 1
    p = posts[0]
    assert p.source == "greenhouse"
    assert p.external_id == "12345"
    assert p.title == "Software Engineer, Backend"
    assert p.location == "New York, NY"
    assert p.url.endswith("/12345")
    # Entities unescaped + tags stripped.
    assert p.description == "Build things ." or p.description == "Build things."
    assert "<" not in p.description and "&lt;" not in p.description
    assert p.posted_at.startswith("2026-01-15")
    assert p.updated_at.startswith("2026-06-01")


def test_lever_parse_epoch_and_plain_description():
    data = [
        {
            "id": "abc-123",
            "text": "Senior SWE",
            "categories": {"location": "Remote"},
            "hostedUrl": "https://jobs.lever.co/acme/abc-123",
            "createdAt": 1735689600000,  # 2025-01-01 UTC
            "descriptionPlain": "Do great work.",
        }
    ]
    posts = lever._parse(data, "acme")
    assert len(posts) == 1
    p = posts[0]
    assert p.external_id == "abc-123"
    assert p.title == "Senior SWE"
    assert p.location == "Remote"
    assert p.description == "Do great work."
    assert p.posted_at.startswith("2025-01-01")


def test_ashby_parse_marks_remote():
    data = {
        "jobs": [
            {
                "id": "uuid-1",
                "title": "New Grad SWE",
                "location": "San Francisco",
                "isRemote": True,
                "jobUrl": "https://jobs.ashbyhq.com/acme/uuid-1",
                "publishedAt": "2026-01-02T00:00:00Z",
                "updatedAt": "2026-06-01T00:00:00Z",
                "descriptionPlain": "Entry level role.",
            }
        ]
    }
    posts = ashby._parse(data, "acme")
    assert len(posts) == 1
    p = posts[0]
    assert p.external_id == "uuid-1"
    assert "remote" in p.location.lower()
    assert p.description == "Entry level role."
    assert p.posted_at.startswith("2026-01-02")
    assert p.updated_at.startswith("2026-06-01")


def test_workable_parse_remote_and_skips_empty():
    import json
    from pathlib import Path

    data = json.loads((Path(__file__).parent / "fixtures" / "workable_jobs.json").read_text())
    posts = workable._parse(data, "acme")
    assert len(posts) == 1
    p = posts[0]
    assert p.source == "workable"
    assert p.external_id == "ABCDEF"
    assert p.company == "Acme"
    assert "remote" in p.location.lower()
    assert p.url.endswith("/ABCDEF/")
    assert "Engineering" in p.description


def test_smartrecruiters_parse_builds_apply_url():
    import json
    from pathlib import Path

    data = json.loads(
        (Path(__file__).parent / "fixtures" / "smartrecruiters_postings.json").read_text()
    )
    posts = smartrecruiters._parse(data, "ServiceNow")
    assert len(posts) == 1
    p = posts[0]
    assert p.source == "smartrecruiters"
    assert p.external_id == "744000146269339"
    assert p.company == "ServiceNow"
    assert p.url == "https://jobs.smartrecruiters.com/ServiceNow/744000146269339"
    assert "remote" in p.location.lower()
    assert "Entry Level" in p.description


def test_yc_parse_inertia_html():
    html = (Path(__file__).parent / "fixtures" / "yc_jobs.html").read_text()
    posts = yc.parse_inertia_html(html)
    assert len(posts) == 2
    accord = next(p for p in posts if p.company == "Accord")
    assert accord.source == "yc"
    assert accord.external_id == "105755"
    assert accord.url.startswith("https://www.ycombinator.com/companies/accord/")
    assert "Revenue Excellence" in accord.description
    assert "W20" in accord.description


def test_rss_himalayas_and_remotive_company_tags():
    from pathlib import Path

    him = rss._parse_xml(
        (Path(__file__).parent / "fixtures" / "rss_himalayas.xml").read_bytes(),
        "himalayas",
        "Himalayas (Remote)",
    )
    assert len(him) == 1
    assert him[0].company == "Quantum Metric"
    assert "United Kingdom" in him[0].location
    rem = rss._parse_xml(
        (Path(__file__).parent / "fixtures" / "rss_remotive.xml").read_bytes(),
        "remotive",
        "Remotive (Remote)",
    )
    assert rem[0].company == "Lemon.io"
    assert "UTC" in rem[0].location or "Remote" in rem[0].location


def test_wwr_category_feed_parses_company_from_colon_title():
    xml = b"""<?xml version="1.0"?>
<rss><channel>
<item>
  <title>Acme Co: Marketing Manager</title>
  <link>https://weworkremotely.com/remote-jobs/1</link>
  <guid>https://weworkremotely.com/remote-jobs/1</guid>
  <region>Anywhere in the World</region>
  <description>Sell things remotely.</description>
</item>
</channel></rss>"""
    posts = rss._parse_xml(xml, "wwr-sales", "We Work Remotely (Sales)")
    assert len(posts) == 1
    assert posts[0].company == "Acme Co"
    assert posts[0].external_id.startswith("wwr-sales:")
    assert "Remote" in posts[0].location


def test_feeds_for_profile_text_picks_wwr_categories():
    assert rss.feeds_for_profile_text("marketing coordinator") == ["wwr-sales"]
    assert "weworkremotely" in rss.feeds_for_profile_text("new grad SWE")
    assert rss.feeds_for_profile_text("product manager") == ["wwr-product"]
    assert rss.feeds_for_profile_text("ux designer") == ["wwr-design"]
    assert rss.feeds_for_profile_text("") == []


def test_swelist_newgrad_description_kind():
    posts = swelist._parse(
        [{
            "id": "ng-1",
            "company_name": "Acme",
            "title": "Software Engineer, New Grad",
            "url": "https://jobs.ashbyhq.com/acme/abc",
            "locations": ["SF"],
            "category": "Software",
            "active": True,
            "is_visible": True,
            "date_posted": 1787529600,
            "date_updated": 1787529600,
        }],
        list_id="newgrad",
        max_age_days=0,
    )
    assert len(posts) == 1
    assert posts[0].external_id.startswith("newgrad:")
    assert "New-grad" in posts[0].description


# NOTE: rss parsing is covered by tests/test_wide_discovery.py
# (fixture-based), since those adapters are feed-oriented.


def test_swelist_proxy_url_detection():
    assert swelist.is_proxy_apply_url(
        "https://job-boards.greenhouse.io/internshiplist2000/jobs/1"
    )
    assert swelist.is_proxy_apply_url("https://simplify.jobs/p/abc")
    assert not swelist.is_proxy_apply_url(
        "https://job-boards.greenhouse.io/astranis/jobs/4601134006"
    )
    assert not swelist.is_proxy_apply_url("https://jobs.lever.co/voltus/abc/apply")


def test_parsers_tolerate_garbage():
    assert greenhouse._parse(None, "x") == []
    assert greenhouse._parse({"jobs": [{"title": "no id"}]}, "x") == []
    assert lever._parse({"not": "a list"}, "x") == []
    assert lever._parse(["string-not-dict"], "x") == []
    assert ashby._parse({}, "x") == []
    assert rss._parse_xml(b"", "x", "x") == []
    assert rss._parse_xml(b"<not-xml", "x", "x") == []
    assert swelist._parse(None, list_id="summer2027") == []
    assert swelist._parse({"not": "a list"}, list_id="summer2027") == []
    assert swelist._parse(["string-not-dict"], list_id="summer2027") == []
    assert workable._parse(None, "x") == []
    assert workable._parse({"jobs": [{"title": "no id"}]}, "x") == []
    assert smartrecruiters._parse(None, "x") == []
    assert smartrecruiters._parse({"content": "nope"}, "x") == []
    assert yc.parse_inertia_html("") == []
    assert yc.parse_inertia_html("<html></html>") == []


def test_fetch_source_unknown_returns_empty():
    assert fetch_source("monster", "acme") == []
