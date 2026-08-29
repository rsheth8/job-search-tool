"""Catalog name → live ATS board probing (mocked fetch, no network)."""
from __future__ import annotations

from app import catalog_probe, config, jobstore
from app.jobsources import JobPosting


def test_probe_learns_a_live_board(monkeypatch):
    monkeypatch.setenv("JOB_CATALOG_PROBE_ENABLED", "true")
    config.get_settings.cache_clear()
    monkeypatch.setattr(
        "app.catalog.names_for_sectors", lambda sectors: ["Acme Corp"]
    )
    monkeypatch.setattr("app.catalog.lookup_board", lambda name: None)
    monkeypatch.setattr(
        "app.catalog.directory_sectors", lambda prof: frozenset({"software"})
    )

    def fetch(source, token):
        if source == "greenhouse" and token in ("acmecorp", "acme-corp", "acme"):
            return [JobPosting(
                "greenhouse", "1", "SWE",
                "https://boards.greenhouse.io/acme/jobs/1", company="Acme",
            )]
        return []

    monkeypatch.setattr("app.catalog_probe.fetch_source", fetch)
    n = catalog_probe.probe_for_user("u", None)
    assert n == 1
    assert jobstore.list_learned_boards()[0][0] == "greenhouse"


def test_probe_skips_names_already_in_the_catalog(monkeypatch):
    monkeypatch.setenv("JOB_CATALOG_PROBE_ENABLED", "true")
    config.get_settings.cache_clear()
    monkeypatch.setattr(
        "app.catalog.names_for_sectors", lambda sectors: ["Stripe"]
    )
    monkeypatch.setattr(
        "app.catalog.lookup_board",
        lambda name: {"source": "greenhouse", "board_token": "stripe"},
    )
    monkeypatch.setattr(
        "app.catalog.directory_sectors", lambda prof: frozenset({"software"})
    )
    called = []
    monkeypatch.setattr(
        "app.catalog_probe.fetch_source",
        lambda source, token: called.append((source, token)) or [],
    )
    assert catalog_probe.probe_for_user("u", None) == 0
    assert called == []
    assert jobstore.list_learned_boards() == []
