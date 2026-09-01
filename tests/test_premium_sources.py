"""Workday / Amazon / USAJobs adapters + paste-a-link ingest (offline)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import apply_queue, catalog, discovery, jobstore, profile, wide_discovery
from app.engine import handle_sms
from app.jobsources import amazon, ingest, netflix, usajobs, workday
from app.jobsources.base import JobPosting
from app.main import app


def test_workday_parse_board_from_careers_url():
    b = workday.parse_board(
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
    )
    assert b is not None
    assert b.host == "nvidia.wd5.myworkdayjobs.com"
    assert b.tenant == "nvidia"
    assert b.site == "NVIDIAExternalCareerSite"
    assert "wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs" in b.jobs_api


def test_workday_parse_board_rejects_non_workday():
    assert workday.parse_board("https://boards.greenhouse.io/acme") is None
    assert workday.parse_board("stripe") is None


def test_workday_parse_listing_fixture():
    board = workday.parse_board(
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite",
        name="NVIDIA",
    )
    data = {
        "total": 1,
        "jobPostings": [
            {
                "title": "Software Engineer, GPU",
                "externalPath": "/job/Santa-Clara/Software-Engineer-GPU_JR12345",
                "locationsText": "Santa Clara, CA",
                "postedOn": "Posted 2 Days Ago",
                "bulletFields": ["JR12345"],
            },
            {"title": "No path"},
        ],
    }
    posts = workday._parse(data, board)
    assert len(posts) == 1
    p = posts[0]
    assert p.source == "workday"
    assert p.external_id == "JR12345"
    assert p.company == "NVIDIA"
    assert "Santa Clara" in p.location
    assert p.url.endswith("/job/Santa-Clara/Software-Engineer-GPU_JR12345")


def test_workday_parsers_tolerate_garbage():
    board = workday.parse_board(
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
    )
    assert workday._parse(None, board) == []
    assert workday._parse({"jobPostings": "nope"}, board) == []
    assert workday.fetch("not-a-url") == []


def test_workday_parse_job_url_no_network():
    p = workday.parse_job_url(
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite"
        "/job/Santa-Clara/Software-Engineer_JR9"
    )
    assert p is not None
    assert p.source == "workday"
    assert p.company == "NVIDIA"
    assert "Software Engineer" in p.title
    assert p.external_id.endswith("JR9") or "JR9" in p.external_id


def test_workday_parse_cisco_and_target_urls():
    cisco = workday.parse_board(
        "https://cisco.wd5.myworkdayjobs.com/en-US/Cisco_Careers"
    )
    assert cisco is not None
    assert cisco.tenant == "cisco"
    assert cisco.site == "Cisco_Careers"
    target = workday.parse_board(
        "https://target.wd5.myworkdayjobs.com/en-US/targetcareers"
    )
    assert target is not None
    assert target.site == "targetcareers"


def test_workday_lookup_nvidia_from_curated_file():
    hit = workday.lookup_company("NVIDIA")
    assert hit is not None
    assert hit["name"] == "NVIDIA"
    assert "myworkdayjobs.com" in hit["token"]


def test_workday_curated_file_includes_cisco():
    hit = workday.lookup_company("Cisco")
    assert hit is not None
    assert hit["name"] == "Cisco"
    assert "Cisco_Careers" in hit["token"]


def test_directory_includes_new_top_company_boards():
    from pathlib import Path
    import json

    data = json.loads(Path("data/ats_boards.json").read_text(encoding="utf-8"))
    gh = {t.lower() for t in data["greenhouse"]}
    assert {"janestreet", "xai", "neuralink", "twitch", "samsara"} <= gh
    hit = catalog.lookup_board("Jane Street")
    assert hit is not None
    assert hit["board_token"] == "janestreet"
    assert workday.lookup_company("Target") is not None
    assert workday.lookup_company("Morgan Stanley") is not None
    assert workday.lookup_company("Accenture") is not None



def test_resolve_board_nvidia_without_live_fetch(monkeypatch):
    monkeypatch.setattr("app.discovery.fetch_source", lambda source, token: [])
    board = discovery.resolve_board("NVIDIA")
    assert board is not None
    assert board["source"] == "workday"
    assert board["company_name"] == "NVIDIA"
    assert board["count"] == 0


def test_amazon_parse_search_json():
    data = {
        "hits": 1,
        "jobs": [
            {
                "id": "123",
                "id_icims": "JOB123",
                "title": "Software Development Engineer",
                "location": "Seattle, Washington, USA",
                "job_path": "/jobs/123/software-development-engineer",
                "description_short": "Build services in Java.",
                "posted_date": "February 1, 2026",
                "company_name": "Amazon",
            }
        ],
    }
    posts = amazon._parse(data, "software engineer")
    assert len(posts) == 1
    assert posts[0].source == "amazon"
    assert posts[0].external_id == "JOB123"
    assert "Seattle" in posts[0].location
    assert posts[0].url.endswith("/software-development-engineer")


def test_amazon_parse_job_url():
    p = amazon.parse_job_url(
        "https://www.amazon.jobs/en/jobs/2612345/software-development-engineer"
    )
    assert p is not None
    assert p.company == "Amazon"
    assert p.external_id == "2612345"
    assert "Software Development Engineer" in p.title


def test_usajobs_parse_and_fetch_without_key():
    data = {
        "SearchResult": {
            "SearchResultItems": [
                {
                    "MatchedObjectId": "1",
                    "MatchedObjectDescriptor": {
                        "PositionID": "DE-123",
                        "PositionTitle": "IT Specialist",
                        "PositionURI": "https://www.usajobs.gov/job/1",
                        "PositionLocationDisplay": "Washington, DC",
                        "OrganizationName": "Department of Commerce",
                        "PublicationStartDate": "2026-08-01",
                        "UserArea": {"Details": {"JobSummary": "Write Python."}},
                    },
                }
            ]
        }
    }
    posts = usajobs._parse(data)
    assert posts[0].source == "usajobs"
    assert posts[0].external_id == "DE-123"
    assert usajobs.fetch("software engineer") == []  # no key in tests


def test_ingest_linkedin_and_indeed_without_fetching():
    li = ingest.from_url("https://www.linkedin.com/jobs/view/4299901234/?ref=x")
    assert li.source == "link"
    assert "4299901234" in li.external_id
    ind = ingest.from_url("https://www.indeed.com/viewjob?jk=abc123")
    assert ind.source == "link"
    assert "abc123" in ind.external_id


def test_ingest_is_job_link_message():
    assert ingest.is_job_link_message("https://www.linkedin.com/jobs/view/1")
    assert ingest.is_job_link_message("save this https://www.amazon.jobs/en/jobs/1/sde")
    assert not ingest.is_job_link_message("applied stripe swe")
    assert not ingest.is_job_link_message(
        "note stripe recruiter sent https://boards.greenhouse.io/acme/jobs/1 and said hi"
    )


def test_chat_paste_linkedin_url_stages_it():
    reply = handle_sms("u", "https://www.linkedin.com/jobs/view/555")
    assert "Saved" in reply or "LinkedIn" in reply
    assert "Apply" in reply
    rows = jobstore.list_postings("u")
    assert len(rows) == 1
    assert apply_queue.list_queue("u")


def test_apply_import_url_endpoint():
    client = TestClient(app)
    r = client.post(
        "/apply/import/url",
        json={"user": "u1", "url": "https://www.amazon.jobs/en/jobs/99/sde"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["posting_id"]
    assert body["source"] == "amazon"


def test_wide_collect_amazon_uses_profile_role(monkeypatch):
    seen: list[str] = []

    def fake_fetch(token):
        seen.append(token)
        return [
            JobPosting(
                "amazon", "1", "SDE", "https://www.amazon.jobs/en/jobs/1",
                company="Amazon", description="software engineer java",
            )
        ]

    monkeypatch.setenv("JOB_SOURCES_ENABLED", "amazon")
    monkeypatch.setenv("JOB_WIDE_AMAZON_ENABLED", "true")
    from app import config

    config.get_settings.cache_clear()
    monkeypatch.setattr(
        "app.wide_discovery.fetch_source",
        lambda src, tok: fake_fetch(tok) if src == "amazon" else [],
    )

    profile.set_profile("u", roles="machine learning intern", keywords="python")
    fresh = wide_discovery.collect_fresh("u", profile.get_profile("u"))
    assert seen == ["machine learning intern"]
    assert fresh[0].company == "Amazon"


def test_workday_directory_batch_rotates(monkeypatch):
    sample = [
        JobPosting("workday", "JR1", "SWE", "https://x/1", company="NVIDIA"),
    ]
    monkeypatch.setattr("app.jobsources.workday.fetch", lambda t: sample)
    monkeypatch.setattr(
        "app.jobsources.workday.load_boards",
        lambda: [{"name": "NVIDIA", "token": "nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite",
                  "url": "https://x", "sectors": {"software"}}],
    )
    jobstore.set_directory_cursor(0, "workday:u:software")
    batch = workday.fetch_directory_batch(user_id="u", sectors=frozenset({"software"}))
    assert len(batch) >= 1
    assert batch[0].external_id.startswith("workday:")


def test_netflix_parse_eightfold_json():
    data = {
        "count": 1,
        "positions": [
            {
                "id": 790318105221,
                "name": "Software Engineer, Playback",
                "posting_name": "Software Engineer, Playback",
                "location": "Los Gatos, California, United States of America",
                "locations": ["Los Gatos, California, United States of America"],
                "department": "Engineering",
                "ats_job_id": "JR42308",
                "canonicalPositionUrl": (
                    "https://explore.jobs.netflix.net/careers/job/790318105221"
                ),
                "t_create": 1700000000,
                "job_description": "Build the playback stack.",
            }
        ],
    }
    posts = netflix._parse(data)
    assert len(posts) == 1
    p = posts[0]
    assert p.source == "netflix"
    assert p.external_id == "790318105221"
    assert p.company == "Netflix"
    assert "Playback" in p.title
    assert "Los Gatos" in p.location
    assert p.url.endswith("/careers/job/790318105221")
    assert p.posted_at.startswith("2023-")


def test_netflix_parsers_tolerate_garbage():
    assert netflix._parse(None) == []
    assert netflix._parse({"positions": "nope"}) == []
    assert netflix.parse_job_url("https://boards.greenhouse.io/acme") is None


def test_netflix_parse_job_url():
    p = netflix.parse_job_url(
        "https://explore.jobs.netflix.net/careers/job/790318105221"
    )
    assert p is not None
    assert p.source == "netflix"
    assert p.external_id == "790318105221"
    assert p.company == "Netflix"


def test_resolve_board_netflix_without_live_fetch(monkeypatch):
    monkeypatch.setattr("app.discovery.fetch_source", lambda source, token: [])
    board = discovery.resolve_board("Netflix")
    assert board is not None
    assert board["source"] == "netflix"
    assert board["company_name"] == "Netflix"
    assert board["count"] == 0


def test_wide_collect_netflix_uses_profile_role(monkeypatch):
    seen: list[str] = []

    def fake_fetch(token):
        seen.append(token)
        return [
            JobPosting(
                "netflix", "1", "SWE",
                "https://explore.jobs.netflix.net/careers/job/1",
                company="Netflix", description="software engineer python",
            )
        ]

    monkeypatch.setenv("JOB_SOURCES_ENABLED", "netflix")
    monkeypatch.setenv("JOB_WIDE_NETFLIX_ENABLED", "true")
    from app import config

    config.get_settings.cache_clear()
    monkeypatch.setattr(
        "app.wide_discovery.fetch_source",
        lambda src, tok: fake_fetch(tok) if src == "netflix" else [],
    )

    profile.set_profile("u", roles="machine learning intern", keywords="python")
    fresh = wide_discovery.collect_fresh("u", profile.get_profile("u"))
    assert seen == ["machine learning intern"]
    assert fresh[0].company == "Netflix"


def test_ingest_netflix_url():
    p = ingest.from_url("https://explore.jobs.netflix.net/careers/job/99")
    assert p is not None
    assert p.source == "netflix"
    assert p.external_id == "99"
