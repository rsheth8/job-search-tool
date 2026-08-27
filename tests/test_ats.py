"""URL -> ATS labeling + autosubmit eligibility."""
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
    assert ats.may_autosubmit(url) is True


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
def test_not_known_ats(url):
    assert ats.ats_of(url) is None
    assert ats.is_fillable_form(url) is False


@pytest.mark.parametrize("url", [
    "https://example.com/careers/apply",
    "https://careers.instacart.com/jobs/123",
    "http://localhost:8000/form",
])
def test_may_autosubmit_any_http(url):
    assert ats.may_autosubmit(url) is True


@pytest.mark.parametrize("url", ["", None, "mailto:hi@x.com", "javascript:alert(1)"])
def test_may_autosubmit_rejects_junk(url):
    assert ats.may_autosubmit(url) is False
