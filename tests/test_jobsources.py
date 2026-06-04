"""Adapter parsing tests — pure, offline, fixture JSON (no network)."""
from __future__ import annotations

from app.jobsources import ashby, fetch_source, greenhouse, lever, rss


def test_greenhouse_parse_normalizes_fields_and_strips_html():
    data = {
        "jobs": [
            {
                "id": 12345,
                "title": "Software Engineer, Backend",
                "location": {"name": "New York, NY"},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/12345",
                "updated_at": "2026-06-01T10:00:00-04:00",
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


def test_rss_parse_items():
    xml = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <title>Who is hiring</title>
      <item>
        <title>Backend Engineer (Remote)</title>
        <link>https://example.com/jobs/1</link>
        <guid>job-1</guid>
        <description>&lt;p&gt;Python &amp; APIs&lt;/p&gt;</description>
        <pubDate>Mon, 01 Jun 2026 10:00:00 GMT</pubDate>
      </item>
    </channel></rss>"""
    posts = rss._parse(xml, "https://example.com/feed")
    assert len(posts) == 1
    p = posts[0]
    assert p.source == "rss"
    assert p.external_id == "job-1"
    assert p.title == "Backend Engineer (Remote)"
    assert p.url == "https://example.com/jobs/1"
    assert "Python" in p.description and "<" not in p.description


def test_rss_parse_atom_entries():
    xml = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Data Scientist</title>
        <link href="https://example.com/a/2"/>
        <id>urn:job:2</id>
        <summary>ML role</summary>
        <updated>2026-06-02T12:00:00Z</updated>
      </entry>
    </feed>"""
    posts = rss._parse(xml, "https://example.com/atom")
    assert len(posts) == 1
    p = posts[0]
    assert p.external_id == "urn:job:2"
    assert p.title == "Data Scientist"
    assert p.url == "https://example.com/a/2"
    assert p.posted_at.startswith("2026-06-02")


def test_parsers_tolerate_garbage():
    assert greenhouse._parse(None, "x") == []
    assert greenhouse._parse({"jobs": [{"title": "no id"}]}, "x") == []
    assert lever._parse({"not": "a list"}, "x") == []
    assert lever._parse(["string-not-dict"], "x") == []
    assert ashby._parse({}, "x") == []
    assert rss._parse("", "x") == []
    assert rss._parse("<not-xml", "x") == []


def test_fetch_source_unknown_returns_empty():
    assert fetch_source("monster", "acme") == []
