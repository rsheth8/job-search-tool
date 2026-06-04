"""Adapter parsing tests — pure, offline, fixture JSON (no network)."""
from __future__ import annotations

from app.jobsources import aggregator, ashby, fetch_source, greenhouse, lever, rss


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


# NOTE: rss + aggregator parsing is covered by tests/test_wide_discovery.py
# (fixture-based), since those adapters are feed/search-oriented.


def test_parsers_tolerate_garbage():
    assert greenhouse._parse(None, "x") == []
    assert greenhouse._parse({"jobs": [{"title": "no id"}]}, "x") == []
    assert lever._parse({"not": "a list"}, "x") == []
    assert lever._parse(["string-not-dict"], "x") == []
    assert ashby._parse({}, "x") == []
    assert rss._parse_xml(b"", "x", "x") == []
    assert rss._parse_xml(b"<not-xml", "x", "x") == []
    assert aggregator._parse(None, "x") == []
    assert aggregator._parse({"jobs_results": ["nope"]}, "x") == []
    assert aggregator._parse({"jobs_results": [{}]}, "x") == []


def test_fetch_source_unknown_returns_empty():
    assert fetch_source("monster", "acme") == []
