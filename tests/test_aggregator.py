"""Paid aggregator (Phase 3): gating, daily budget cap, discovery integration.

All offline — httpx is stubbed, so no test ever makes a real (billable) call.
"""
from __future__ import annotations

from app import config, discovery, jobstore, profile
from app.jobsources import JobPosting, aggregator


def _activate(monkeypatch, **overrides):
    """Turn the aggregator on (flag + key) plus any per-test setting overrides."""
    monkeypatch.setenv("AGGREGATOR_API_KEY", "test-key")
    monkeypatch.setenv("AGGREGATOR_SEARCH_ENABLED", "true")
    for k, v in overrides.items():
        monkeypatch.setenv(k, str(v))
    config.get_settings.cache_clear()


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# --- gating ----------------------------------------------------------------

def test_fetch_disabled_by_default_makes_no_call(monkeypatch):
    called = []
    monkeypatch.setattr("httpx.get", lambda *a, **k: called.append(1))
    # conftest leaves it inactive (no key, flag off).
    assert aggregator.fetch("swe") == []
    assert called == []


def test_fetch_requires_both_flag_and_key(monkeypatch):
    monkeypatch.setenv("AGGREGATOR_API_KEY", "k")  # key set but flag still false
    config.get_settings.cache_clear()
    assert config.get_settings().aggregator_active is False
    assert aggregator.fetch("swe") == []


def test_fetch_empty_query_is_noop(monkeypatch):
    _activate(monkeypatch)
    called = []
    monkeypatch.setattr("httpx.get", lambda *a, **k: called.append(1))
    assert aggregator.fetch("  ") == []
    assert called == []


# --- happy path + budget ---------------------------------------------------

def test_fetch_happy_path_records_call(monkeypatch):
    _activate(monkeypatch)
    payload = {"jobs_results": [
        {"title": "SWE", "company_name": "Acme", "job_id": "1",
         "apply_options": [{"link": "https://x/1"}]},
    ]}
    monkeypatch.setattr("httpx.get", lambda *a, **k: _FakeResp(payload))
    posts = aggregator.fetch("software engineer")
    assert len(posts) == 1 and posts[0].company == "Acme"
    assert aggregator._calls_today() == 1
    assert aggregator.usage()["searches"] == 1


def test_fetch_respects_daily_cap(monkeypatch):
    _activate(monkeypatch, AGGREGATOR_MAX_CALLS_PER_DAY=0)
    called = []
    monkeypatch.setattr("httpx.get", lambda *a, **k: called.append(1))
    assert aggregator.fetch("swe") == []
    assert called == []  # never reached the network
    assert aggregator.usage()["skipped_daily_cap"] == 1


def test_fetch_errors_degrade_to_empty_without_burning_budget(monkeypatch):
    _activate(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr("httpx.get", boom)
    assert aggregator.fetch("swe") == []
    assert aggregator.usage()["errors"] == 1
    assert aggregator._calls_today() == 0  # a failed call isn't counted


def test_fetch_caps_results(monkeypatch):
    _activate(monkeypatch, AGGREGATOR_RESULTS_PER_CALL=1)
    payload = {"jobs_results": [
        {"title": "A", "job_id": "1", "apply_options": [{"link": "https://x/1"}]},
        {"title": "B", "job_id": "2", "apply_options": [{"link": "https://x/2"}]},
    ]}
    monkeypatch.setattr("httpx.get", lambda *a, **k: _FakeResp(payload))
    assert len(aggregator.fetch("swe")) == 1


# --- query building --------------------------------------------------------

def test_profile_query_includes_location():
    profile.set_profile("u", roles="new grad swe", keywords="swe", locations="remote, nyc")
    q = discovery._profile_query(profile.get_profile("u"))
    assert "new grad swe" in q and "remote" in q and "in nyc" in q


def test_profile_query_empty_without_roles():
    assert discovery._profile_query(None) == ""
    profile.set_profile("u", keywords="swe", locations="remote")
    assert discovery._profile_query(profile.get_profile("u")) == ""


# --- discovery integration -------------------------------------------------

def test_discovery_baselines_then_alerts_new(monkeypatch):
    _activate(monkeypatch)
    profile.set_profile("u", roles="backend engineer", keywords="python", locations="remote")

    feed = [JobPosting("aggregator", "1", "Backend Engineer", "https://x/1",
                       company="Acme", description="python backend")]
    monkeypatch.setattr(
        "app.discovery.fetch_source",
        lambda s, t: feed if s == "aggregator" else [],
    )

    class Cap:
        def __init__(self):
            self.sent = []

        def send(self, u, b):
            self.sent.append(b)

    cap = Cap()
    # First tick baselines silently — no alert, posting stored as 'seeded'.
    assert discovery.tick("u", sender=cap) == 0
    assert jobstore.counts_by_status("u").get("seeded") == 1
    assert cap.sent == []

    # A genuinely new posting after baseline → alerts.
    feed.append(JobPosting("aggregator", "2", "Senior Backend Engineer", "https://x/2",
                           company="Acme", description="python backend"))
    assert discovery.tick("u", sender=cap) == 1
    assert len(cap.sent) == 1


def test_discovery_skips_aggregator_when_inactive(monkeypatch):
    # conftest leaves it inactive; even with a profile the pass is a no-op.
    profile.set_profile("u", roles="swe", keywords="swe", locations="remote")
    calls = []
    monkeypatch.setattr(
        "app.discovery.fetch_source",
        lambda s, t: calls.append((s, t)) or [],
    )
    assert discovery.tick("u", sender=None) == 0
    assert all(s != "aggregator" for s, _ in calls)


def test_run_all_sweeps_profile_users_when_active(monkeypatch):
    _activate(monkeypatch)
    profile.set_profile("solo", roles="swe", keywords="swe", locations="remote")
    # 'solo' tracks no boards — only run_all's profile-user union reaches them.
    assert "solo" in discovery._discovery_users(config.get_settings())
