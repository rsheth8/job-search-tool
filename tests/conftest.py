import os
import tempfile

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_slack: live Slack API smoke test (needs SLACK_BOT_TOKEN in .env)",
    )


@pytest.fixture(autouse=True)
def temp_db(monkeypatch):
    """Give every test a fresh SQLite file and force the heuristic router."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")  # force offline heuristic router
    monkeypatch.setenv("APOLLO_API_KEY", "")  # never hit live Apollo from .env
    monkeypatch.setenv("SERPAPI_API_KEY", "")  # never hit the paid aggregator
    monkeypatch.setenv("VOYAGE_API_KEY", "")  # never hit the paid embedder
    monkeypatch.setenv("EMBEDDING_ENABLED", "false")
    monkeypatch.setenv("RERANKER_ENABLED", "false")  # opt-in; tests enable explicitly
    monkeypatch.setenv("ELIGIBILITY_FILTER_ENABLED", "false")  # opt-in; tests enable explicitly
    monkeypatch.setenv("ELIGIBILITY_LLM_ENABLED", "false")
    monkeypatch.setenv("DECK_TLDR_ENABLED", "false")  # opt-in; tests inject a summarizer
    # Keep wide discovery off by default so tests never touch the network; tests
    # that exercise it enable + monkeypatch the fetchers explicitly.
    monkeypatch.setenv("JOB_WIDE_AGGREGATOR_ENABLED", "false")
    monkeypatch.setenv("JOB_WIDE_RSS_ENABLED", "false")
    monkeypatch.setenv("JOB_WIDE_DIRECTORY_ENABLED", "false")
    monkeypatch.setenv("JOB_WIDE_SWELIST_ENABLED", "false")
    # Neutralize live Slack tokens from .env so the webhook tests post unsigned
    # (no real signing secret) and no test ever calls the Slack Web API. Tests
    # that exercise signing/outbound set these explicitly via monkeypatch.
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "")
    monkeypatch.setenv("SLACK_TRANSPORT_ENABLED", "false")
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "false")
    monkeypatch.setenv("AUTH_LEGACY_USER_ID", "")
    monkeypatch.setenv("AUTH_FAIL_OPEN", "true")
    monkeypatch.setenv("AUTH_ALLOWED_EMAILS", "")
    monkeypatch.setenv("APPLY_AUTOSUBMIT_ENABLED", "true")
    monkeypatch.setenv("SENTRY_DSN", "")
    monkeypatch.setenv("LLM_MAX_CALLS_PER_USER_PER_DAY", "0")
    monkeypatch.setenv("RESUME_TAILOR_ENABLED", "false")
    # Autofill/mobile endpoints are token-gated when APPLY_API_TOKEN is set in
    # .env (prod/local). Clear it so suite requests without X-Apply-Token stay
    # open; tests that exercise the gate set the token explicitly.
    monkeypatch.setenv("APPLY_API_TOKEN", "")
    # Twilio outbound is .env-configured in prod; neutralize it so the LogSender/
    # config tests stay hermetic regardless of a real number in .env (the tests
    # that exercise Twilio set these explicitly via monkeypatch).
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "")
    # Neutralize the .env fallback in get_settings so a real key in .env can't
    # pull tests onto the live (paid) API — tests must stay offline.
    monkeypatch.setattr("dotenv.dotenv_values", lambda *a, **k: {})

    # Reset cached settings + router singleton so env changes take effect.
    from app import (
        apollo, auth, config, eligibility, embeddings, insights, llm_budget,
        matcher, reminders, router,
    )

    config.get_settings.cache_clear()
    router._router_singleton = None
    reminders._sender_singleton = None  # don't leak a sender across tests
    apollo._client_singleton = None  # nor an Apollo client
    apollo._last_discovery_issue = None
    apollo.reset_for_tests()
    auth.reset_for_tests()
    matcher._llm_client = None  # nor a matcher LLM client/limiter
    matcher._llm_limiter = None
    embeddings.reset_for_tests()  # nor an embedding rate limiter
    eligibility.reset_for_tests()  # nor an eligibility LLM client/limiter
    insights.reset_for_tests()  # nor a deck-TLDR LLM client/limiter
    llm_budget.set_user("")

    from app.db import init_db

    init_db()
    yield
    os.unlink(path)
