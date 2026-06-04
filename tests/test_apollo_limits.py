"""Apollo credit / rate guard tests (offline, no network)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app import apollo
from app.config import get_settings


@pytest.fixture
def apollo_client(monkeypatch):
    """Fake client that succeeds without HTTP."""
    client = MagicMock()
    client.find_people.return_value = [
        {"name": "Jane Doe", "title": "Recruiter", "company": "Stripe"},
    ]
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")
    get_settings.cache_clear()
    apollo.reset_for_tests()
    monkeypatch.setattr(apollo, "get_apollo", lambda: client)
    return client


def test_daily_cap_blocks_discovery(monkeypatch, apollo_client):
    monkeypatch.setenv("APOLLO_MAX_DISCOVERIES_PER_DAY", "1")
    get_settings.cache_clear()
    apollo.reset_for_tests()
    monkeypatch.setattr(apollo, "get_apollo", lambda: apollo_client)

    assert len(apollo.discover_recruiters("Stripe")) == 1
    assert apollo_client.find_people.call_count == 1

    assert apollo.discover_recruiters("Ramp") == []
    assert "limit for today" in (apollo.discovery_issue() or "").lower()
    assert apollo_client.find_people.call_count == 1  # no second HTTP call


def test_rate_limit_blocks_discovery(monkeypatch, apollo_client):
    monkeypatch.setenv("APOLLO_MAX_DISCOVERIES_PER_DAY", "100")
    monkeypatch.setenv("APOLLO_RATE_LIMIT_PER_MIN", "1")
    get_settings.cache_clear()
    apollo.reset_for_tests()
    monkeypatch.setattr(apollo, "get_apollo", lambda: apollo_client)

    assert len(apollo.discover_recruiters("Stripe")) == 1
    assert apollo.discover_recruiters("Ramp") == []
    assert "rate limit" in (apollo.discovery_issue() or "").lower()
    assert apollo_client.find_people.call_count == 1


def test_max_results_caps_request(monkeypatch, apollo_client):
    monkeypatch.setenv("APOLLO_MAX_RESULTS", "2")
    get_settings.cache_clear()
    apollo.reset_for_tests()
    monkeypatch.setattr(apollo, "get_apollo", lambda: apollo_client)

    apollo.discover_recruiters("Stripe", limit=10)
    _, kwargs = apollo_client.find_people.call_args
    assert kwargs["limit"] == 2


def test_org_lookup_skipped_when_disabled(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")
    monkeypatch.setenv("APOLLO_ORG_LOOKUP_ENABLED", "false")
    get_settings.cache_clear()
    apollo.reset_for_tests()

    client = apollo.ApolloClient("test-key")
    client._http = MagicMock()
    assert client._resolve_domain("Stripe") is None
    client._http.post.assert_not_called()


def test_usage_tracks_people_searches(monkeypatch, apollo_client):
    monkeypatch.setenv("APOLLO_MAX_DISCOVERIES_PER_DAY", "10")
    get_settings.cache_clear()
    apollo.reset_for_tests()
    monkeypatch.setattr(apollo, "get_apollo", lambda: apollo_client)

    apollo.discover_recruiters("Stripe")
    u = apollo.usage()
    assert u["people_searches"] == 1
    assert u["discoveries_today"] == 1
    assert u["org_lookup_enabled"] is False


def test_people_search_caches_domain_from_org_blob():
    from app.db import connect, init_db

    init_db()
    apollo._cache_domain_from_org_blob(
        "Stripe",
        {"name": "Stripe", "primary_domain": "stripe.com", "id": "org123"},
    )
    assert apollo._get_cached_domain("Stripe") == "stripe.com"


def test_org_miss_blocks_repeat_credit_spend(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "test-key")
    monkeypatch.setenv("APOLLO_ORG_LOOKUP_ENABLED", "true")
    monkeypatch.setenv("APOLLO_MAX_ORG_SEARCHES_PER_DAY", "10")
    get_settings.cache_clear()
    apollo.reset_for_tests()

    apollo._record_org_miss("Obscure Co")
    client = apollo.ApolloClient("test-key")
    client._http = MagicMock()
    assert client._resolve_domain("Obscure Co") is None
    client._http.post.assert_not_called()
