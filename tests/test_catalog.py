"""Sector company catalog — profile mapping + directory filter (offline)."""
from __future__ import annotations

import sqlite3

from app import catalog
from app.jobsources import directory
from app.jobsources import JobPosting


def _profile(roles: str, keywords: str = "") -> sqlite3.Row:
    cols = {"roles": roles, "keywords": keywords, "locations": "",
            "seniority": "", "resume_summary": "", "min_relevance": None}
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    keys = ", ".join(cols)
    conn.execute(f"CREATE TABLE p ({keys})")
    conn.execute(
        f"INSERT INTO p ({keys}) VALUES ({', '.join('?' * len(cols))})",
        tuple(cols.values()),
    )
    return conn.execute("SELECT * FROM p").fetchone()


def test_nurse_profile_selects_healthcare_not_software():
    assert catalog.sectors_for_profile(_profile("registered nurse")) == frozenset(
        {"healthcare"}
    )
    assert catalog.sectors_for_profile(_profile("software engineer")) == frozenset(
        {"software"}
    )
    assert "marketing" in catalog.sectors_for_profile(
        _profile("marketing coordinator", keywords="brand")
    )


def test_empty_profile_defaults_to_software():
    assert catalog.sectors_for_profile(None) == frozenset({"software"})
    assert catalog.sectors_for_profile(_profile("", "")) == frozenset({"software"})


def test_product_and_construction_profiles():
    assert catalog.sectors_for_profile(_profile("product manager")) == frozenset(
        {"product", "software"}
    )
    assert catalog.sectors_for_profile(_profile("construction superintendent")) == frozenset(
        {"construction"}
    )
    assert "support" in catalog.sectors_for_profile(
        _profile("customer success specialist")
    )
    av = catalog.sectors_for_profile(_profile("looking for aviation jobs"))
    assert "aviation" in av
    assert catalog.directory_sectors(_profile("flight attendant")) >= frozenset(
        {"aviation"}
    )


def test_directory_sectors_keeps_software_for_occupation_searches():
    marketing = catalog.directory_sectors(
        _profile("marketing coordinator", keywords="brand")
    )
    assert "software" in marketing
    assert "marketing" in marketing
    assert catalog.directory_sectors(_profile("registered nurse")) == frozenset(
        {"healthcare"}
    )


def test_names_for_sectors_dedupes(monkeypatch):
    monkeypatch.setattr(
        "app.catalog.load",
        lambda: {
            "version": 1,
            "boards": [],
            "names": {
                "healthcare": ["Mayo Clinic", "Cleveland Clinic"],
                "software": ["Stripe", "Mayo Clinic"],
            },
        },
    )
    names = catalog.names_for_sectors(frozenset({"healthcare", "software"}))
    assert names.count("Mayo Clinic") == 1
    assert "Stripe" in names
    assert "Cleveland Clinic" in names


def test_lookup_board_matches_catalog_name(monkeypatch):
    monkeypatch.setattr(
        "app.catalog.load",
        lambda: {
            "version": 1,
            "boards": [{
                "name": "Stripe",
                "source": "greenhouse",
                "token": "stripe",
                "sectors": ["software", "finance"],
            }],
            "names": {},
        },
    )
    hit = catalog.lookup_board("Stripe Inc")
    assert hit is not None
    assert hit["source"] == "greenhouse"
    assert hit["board_token"] == "stripe"


def test_directory_rotates_only_matching_sector(monkeypatch):
    monkeypatch.setattr(
        "app.jobsources.directory._load_boards",
        lambda: {
            "greenhouse": ["stripe"],
            "lever": [],
            "ashby": [],
            "workable": [],
            "smartrecruiters": ["AbbVie"],
        },
    )
    monkeypatch.setattr(
        "app.jobsources.directory.catalog.sector_index",
        lambda: {
            ("greenhouse", "stripe"): {"software"},
            ("smartrecruiters", "AbbVie"): {"healthcare"},
        },
    )
    monkeypatch.setattr(
        "app.jobsources.directory.catalog.probe_pairs",
        lambda sectors: (
            [("smartrecruiters", "AbbVie")]
            if sectors and "healthcare" in sectors
            else []
        ),
    )
    fetched: list[str] = []

    def gh(token):
        fetched.append(f"greenhouse:{token}")
        return [
            JobPosting("greenhouse", "1", "SWE", "https://x/1", company="Stripe"),
        ]

    def sr(token):
        fetched.append(f"smartrecruiters:{token}")
        return [
            JobPosting("smartrecruiters", "2", "Nurse", "https://x/2", company="AbbVie"),
        ]

    monkeypatch.setitem(directory._FETCHERS, "greenhouse", gh)
    monkeypatch.setitem(directory._FETCHERS, "smartrecruiters", sr)
    batch = directory.fetch_directory_batch(
        boards_to_probe=1,
        max_jobs_per_board=5,
        user_id="u",
        sectors=frozenset({"healthcare"}),
    )
    assert fetched == ["smartrecruiters:AbbVie"]
    assert len(batch) == 1
    assert batch[0].company == "AbbVie"


def test_catalog_json_has_large_healthcare_and_education_lists():
    catalog.reset_cache()
    catalog.load.cache_clear()
    healthcare = catalog.name_count("healthcare")
    education = catalog.name_count("education")
    if healthcare == 0 and education == 0:
        import pytest
        pytest.skip("data/company_catalog.json not generated")
    assert healthcare >= 1000
    assert education >= 1000
    assert catalog.stats()["boards"] >= 50
    for sector in ("construction", "aerospace", "automotive", "telecom",
                   "product", "support", "aviation"):
        assert catalog.name_count(sector) >= 10, sector
