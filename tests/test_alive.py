"""Apply-URL liveness — offline, injectable HTTP. No live network."""
from __future__ import annotations

import json

import pytest

from app import ats, jobstore, profile
from app.jobsources import alive, ghost
from app.jobsources.alive import FetchResult
from app.jobsources.base import JobPosting

GH_OPEN = "https://boards.greenhouse.io/acme/jobs/111"
GH_DEAD = "https://boards.greenhouse.io/acme/jobs/222"
GH_JSON_OPEN = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/111"
GH_JSON_DEAD = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/222"
LEVER = "https://jobs.lever.co/acme/abcd-ef01/apply"
ASHBY = "https://jobs.ashbyhq.com/acme/job-uuid-1"
RSS = "https://remoteok.com/jobs/999"

_LIVE_GH = json.dumps({
    "id": 111, "title": "Software Engineer",
    "absolute_url": GH_OPEN,
})
_LIVE_LEVER = json.dumps({
    "id": "abcd-ef01", "text": "Software Engineer",
    "hostedUrl": "https://jobs.lever.co/acme/abcd-ef01",
})


def _p(url: str, ext="1", source="greenhouse") -> JobPosting:
    return JobPosting(
        source=source, external_id=ext, title="SWE", url=url,
        company="Acme", description="Build things.",
    )


def _get(routes: dict[str, FetchResult]):
    """url → result. Unexpected URLs fail the test instead of fail-opening."""
    def get(url, timeout=None):  # noqa: ARG001
        if url in routes:
            return routes[url]
        raise AssertionError(f"unexpected GET {url}")
    return get


# --- JSON is the source of truth for Greenhouse / Lever / SR ---------------

def test_greenhouse_json_404_is_closed():
    is_open, reason = alive.inspect_apply_url(
        GH_DEAD, get=_get({GH_JSON_DEAD: FetchResult(404, "", GH_JSON_DEAD)}),
        use_cache=False,
    )
    assert is_open is False
    assert reason == "json_404"


def test_greenhouse_json_live_skips_html():
    fetched = []

    def get(url, timeout=None):  # noqa: ARG001
        fetched.append(url)
        if url == GH_JSON_OPEN:
            return FetchResult(200, _LIVE_GH, GH_JSON_OPEN)
        raise AssertionError(f"HTML should not be fetched, got {url}")

    is_open, reason = alive.inspect_apply_url(GH_OPEN, get=get, use_cache=False)
    assert is_open is True
    assert reason == "json_live"
    assert fetched == [GH_JSON_OPEN]


def test_lever_json_404_is_closed():
    probe = ats.json_probe_url(LEVER)
    assert probe is not None
    is_open, reason = alive.inspect_apply_url(
        LEVER, get=_get({probe: FetchResult(404, "", probe)}), use_cache=False,
    )
    assert is_open is False
    assert reason == "json_404"


def test_json_status_closed_field_is_dead():
    probe = ats.json_probe_url(LEVER)
    body = json.dumps({"id": "x", "text": "SWE", "state": "closed"})
    is_open, reason = alive.inspect_apply_url(
        LEVER, get=_get({probe: FetchResult(200, body, probe)}), use_cache=False,
    )
    assert is_open is False
    assert reason == "json_dead"


def test_json_500_falls_through_to_html_closed_copy():
    html = "<html><h1>This position has been filled.</h1></html>"

    def get(url, timeout=None):  # noqa: ARG001
        if "boards-api" in url:
            return FetchResult(500, "nope", url)
        return FetchResult(200, html, GH_DEAD)

    is_open, reason = alive.inspect_apply_url(GH_DEAD, get=get, use_cache=False)
    assert is_open is False
    assert reason == "closed_copy"


# --- HTML fallback (Ashby / RSS) ------------------------------------------

@pytest.mark.parametrize("html", [
    "<h1>This position has been filled.</h1>",
    "<p>We are no longer accepting applications for this role.</p>",
    "<div>The job you are looking for is no longer available.</div>",
    "<h1>This job is<br>no longer available</h1>",
    "Job not found.",
])
def test_closed_html_is_dropped(html):
    is_open, reason = alive.inspect_apply_url(
        ASHBY, get=_get({ASHBY: FetchResult(200, html, ASHBY)}), use_cache=False,
    )
    assert is_open is False
    assert reason == "closed_copy"


@pytest.mark.parametrize("html", [
    "<h1>Apply for Software Engineer</h1><button>Submit</button>",
    "<p>Sorry, this job requires 5 years of experience.</p>",
    "<p>Applications are reviewed weekly.</p>",
    "<p>This role is closed to remote applicants; NYC only.</p>",
])
def test_open_html_is_kept(html):
    is_open, reason = alive.inspect_apply_url(
        ASHBY, get=_get({ASHBY: FetchResult(200, html, ASHBY)}), use_cache=False,
    )
    assert is_open is True
    assert reason == "open"


def test_http_404_on_apply_page_is_closed():
    is_open, reason = alive.inspect_apply_url(
        RSS, get=_get({RSS: FetchResult(404, "", RSS)}), use_cache=False,
    )
    assert is_open is False
    assert reason == "http_404"


def test_http_410_is_closed():
    is_open, reason = alive.inspect_apply_url(
        RSS, get=_get({RSS: FetchResult(410, "", RSS)}), use_cache=False,
    )
    assert is_open is False


def test_login_wall_fail_open():
    final = "https://login.microsoftonline.com/common/oauth2"
    is_open, reason = alive.inspect_apply_url(
        ASHBY, get=_get({ASHBY: FetchResult(200, "Sign in", final)}),
        use_cache=False,
    )
    assert is_open is True
    assert reason == "login_wall"


def test_redirect_off_job_on_same_ats_is_closed():
    board = "https://jobs.ashbyhq.com/acme"
    is_open, reason = alive.inspect_apply_url(
        ASHBY, get=_get({ASHBY: FetchResult(200, "<h1>Jobs at Acme</h1>", board)}),
        use_cache=False,
    )
    assert is_open is False
    assert reason == "redirected"


def test_redirect_to_apply_path_still_open():
    final = "https://jobs.ashbyhq.com/acme/job-uuid-1/application"
    is_open, reason = alive.inspect_apply_url(
        ASHBY, get=_get({ASHBY: FetchResult(200, "<form>Apply</h1>", final)}),
        use_cache=False,
    )
    assert is_open is True


def test_network_error_fail_open():
    def boom(url, timeout=None):  # noqa: ARG001
        raise RuntimeError("offline")

    is_open, reason = alive.inspect_apply_url(GH_OPEN, get=boom, use_cache=False)
    assert is_open is True
    assert reason == "error"


def test_empty_url_fail_open():
    is_open, reason = alive.inspect_apply_url("", use_cache=False)
    assert is_open is True
    assert reason == "empty"


def test_403_fail_open():
    is_open, reason = alive.inspect_apply_url(
        RSS, get=_get({RSS: FetchResult(403, "nope", RSS)}), use_cache=False,
    )
    assert is_open is True
    assert "403" in reason


def test_legacy_request_seam_still_works():
    assert not alive.check_apply_url(
        RSS, request=lambda method, url: (404, ""), use_cache=False,
    )


def test_filter_open_counts_drops():
    posts = [_p(RSS, ext="1", source="rss"), _p("https://gone/2", ext="2", source="rss")]
    kept, dropped = alive.filter_open(
        posts, check=lambda url: url.endswith("999"), workers=1,
    )
    assert dropped == 1
    assert [p.url for p in kept] == [RSS]


def test_real_greenhouse_form_fixture_is_open():
    from pathlib import Path

    html = (Path(__file__).parent / "fixtures/forms/greenhouse_basic.html").read_text()
    is_open, reason = alive.inspect_apply_url(
        ASHBY, get=_get({ASHBY: FetchResult(200, html, ASHBY)}), use_cache=False,
    )
    assert is_open is True
    assert reason == "open"


def test_filter_open_fail_open_when_check_raises():
    posts = [_p(RSS, ext="1", source="rss")]
    kept, dropped = alive.filter_open(
        posts, check=lambda url: (_ for _ in ()).throw(RuntimeError("x")), workers=1,
    )
    assert dropped == 0 and kept == posts


# --- page copy: closed vs ordinary -----------------------------------------

def test_page_says_closed_strips_tags():
    assert ghost.page_says_closed("<h1>This job is<br>no longer available</h1>")
    assert not ghost.page_says_closed("Sorry, this job requires 5 years.")
    assert not ghost.page_says_closed("This role is closed to remote applicants.")


def test_is_closed_does_not_treat_closed_to_as_filled():
    p = JobPosting(
        source="greenhouse", external_id="1", title="SWE",
        url=GH_OPEN, description="This role is closed to remote applicants.",
    )
    assert not ghost.is_closed(p)
    assert not ghost.is_ghost(p)


# --- Apply tab re-check ------------------------------------------------------

def test_close_dead_shortlist_marks_and_unstages(monkeypatch):
    from app import apply_queue

    profile.set_profile("u1", roles="backend engineer")
    row = jobstore.save_posting(
        "u1", _p(GH_DEAD, ext="222"), relevance_score=0.9, status="queued",
    )
    assert apply_queue.stage("u1", row["id"])
    monkeypatch.setattr(
        "app.jobsources.alive.http_get",
        _get({GH_JSON_DEAD: FetchResult(404, "", GH_JSON_DEAD)}),
    )
    n = alive.close_dead_shortlist("u1", today_n=5)
    assert n == 1
    assert jobstore.get_posting("u1", row["id"])["status"] == "closed"
    assert apply_queue.list_queue("u1") == []


def test_close_dead_shortlist_keeps_live_jobs(monkeypatch):
    profile.set_profile("u1", roles="backend engineer")
    row = jobstore.save_posting(
        "u1", _p(GH_OPEN, ext="111"), relevance_score=0.9, status="queued",
    )
    monkeypatch.setattr(
        "app.jobsources.alive.http_get",
        _get({GH_JSON_OPEN: FetchResult(200, _LIVE_GH, GH_JSON_OPEN)}),
    )
    assert alive.close_dead_shortlist("u1", today_n=5) == 0
    assert jobstore.get_posting("u1", row["id"])["status"] == "queued"
