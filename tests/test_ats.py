"""URL -> ATS labeling."""
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
    ("https://apply.workable.com/acme/j/ABCDEF/", "workable"),
    ("https://jobs.smartrecruiters.com/ServiceNow/744000146269339", "smartrecruiters"),
    ("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/X/Y", "workday"),
    ("https://www.amazon.jobs/en/jobs/1/sde", "amazon"),
    ("https://explore.jobs.netflix.net/careers/job/790318105221", "netflix"),
    ("https://www.usajobs.gov/job/1", "usajobs"),
])
def test_ats_of_first_party(url, name):
    assert ats.ats_of(url) == name
    if name in ats.FILLABLE_SOURCES:
        assert ats.is_fillable_form(url) is True
    else:
        assert ats.is_fillable_form(url) is False


def test_apply_kind_labels():
    assert ats.apply_kind("https://boards.greenhouse.io/acme/jobs/1") == "autofill"
    assert ats.apply_kind("https://apply.workable.com/acme/j/ABCDEF/") == "direct"
    assert ats.apply_kind(
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/X/Y"
    ) == "direct"
    assert ats.apply_kind("https://www.amazon.jobs/en/jobs/1/sde") == "browser"
    assert ats.apply_kind(
        "https://explore.jobs.netflix.net/careers/job/790318105221"
    ) == "browser"
    assert ats.apply_kind("https://remoteok.com/jobs/1", source="rss") == "browser"
    assert ats.apply_kind("https://example.com/jobs/1", source="greenhouse") == "direct"
    assert ats.apply_kind("https://careers.example.com/apply/1") == "browser"
    assert ats.apply_kind("https://www.linkedin.com/jobs/view/123") == "browser"


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


@pytest.mark.parametrize("url,source,token", [
    ("https://job-boards.greenhouse.io/astranis/jobs/4601134006", "greenhouse", "astranis"),
    ("https://boards.greenhouse.io/embed/job_app?for=stripe&token=1", "greenhouse", "stripe"),
    ("https://jobs.lever.co/voltus/b7833dd8/apply", "lever", "voltus"),
    ("https://jobs.ashbyhq.com/mechanize/1ef28bb2/application", "ashby", "mechanize"),
    ("https://apply.workable.com/grayce/j/B5D022B13D/apply", "workable", "grayce"),
    ("https://jobs.smartrecruiters.com/ServiceNow/744000146269339", "smartrecruiters", "ServiceNow"),
])
def test_board_from_url(url, source, token):
    assert ats.board_from_url(url) == (source, token)


@pytest.mark.parametrize("url", [
    "https://job-boards.greenhouse.io/internshiplist2000/jobs/1",
    "https://simplify.jobs/p/abc",
    "https://www.linkedin.com/jobs/view/123",
    "https://apply.workable.com/j/ABCDEF",  # company slug missing
])
def test_board_from_url_skips_proxies_and_junk(url):
    assert ats.board_from_url(url) is None


@pytest.mark.parametrize("url,source,token,job_id", [
    ("https://job-boards.greenhouse.io/astranis/jobs/4601134006", "greenhouse", "astranis", "4601134006"),
    ("https://boards.greenhouse.io/embed/job_app?for=stripe&token=1", "greenhouse", "stripe", "1"),
    ("https://jobs.lever.co/voltus/b7833dd8/apply", "lever", "voltus", "b7833dd8"),
    ("https://jobs.ashbyhq.com/mechanize/1ef28bb2/application", "ashby", "mechanize", "1ef28bb2"),
    ("https://apply.workable.com/grayce/j/B5D022B13D/apply", "workable", "grayce", "B5D022B13D"),
    ("https://jobs.smartrecruiters.com/ServiceNow/744000146269339", "smartrecruiters", "ServiceNow", "744000146269339"),
])
def test_posting_ref(url, source, token, job_id):
    assert ats.posting_ref(url) == (source, token, job_id)


def test_json_probe_url_for_ats_with_a_job_api():
    assert ats.json_probe_url(
        "https://boards.greenhouse.io/acme/jobs/111"
    ) == "https://boards-api.greenhouse.io/v1/boards/acme/jobs/111"
    assert ats.json_probe_url(
        "https://jobs.lever.co/acme/abcd-ef01/apply"
    ) == "https://api.lever.co/v0/postings/acme/abcd-ef01"
    assert ats.json_probe_url("https://jobs.ashbyhq.com/acme/uuid-1") is None
    assert ats.json_probe_url("https://remoteok.com/jobs/1") is None
