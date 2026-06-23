"""URL -> ATS detection: which links the headless worker may auto-fill (first-party
Greenhouse/Lever/Ashby forms) vs. hand off (aggregator/RSS/login/unknown)."""
from __future__ import annotations

import pytest

from app import ats


@pytest.mark.parametrize("url,name", [
    ("https://boards.greenhouse.io/acme/jobs/123", "greenhouse"),
    ("https://job-boards.greenhouse.io/acme/jobs/123", "greenhouse"),
    ("https://acme.greenhouse.io/jobs/123", "greenhouse"),
    ("https://jobs.lever.co/acme/abc-def", "lever"),
    ("https://jobs.ashbyhq.com/acme/uuid", "ashby"),
    ("HTTPS://Jobs.Lever.CO/Acme/X", "lever"),  # case-insensitive host
])
def test_ats_of_first_party(url, name):
    assert ats.ats_of(url) == name
    assert ats.is_fillable_form(url) is True


@pytest.mark.parametrize("url", [
    # the exact failure that motivated this: a Google-Jobs aggregator redirect
    "https://careersprint.7f.liveblog365.com/job/2432024?utm_campaign=google_jobs_apply",
    "https://www.linkedin.com/jobs/view/123",
    "https://www.indeed.com/viewjob?jk=abc",
    "https://example.com/careers/apply",
    "https://notgreenhouse.io.evil.com/x",   # suffix spoof must not match
    "greenhouse.io/acme",                     # no scheme/host -> not parseable as host
    "",
    None,
])
def test_not_fillable(url):
    assert ats.ats_of(url) is None
    assert ats.is_fillable_form(url) is False
