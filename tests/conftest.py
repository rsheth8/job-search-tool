import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def temp_db(monkeypatch):
    """Give every test a fresh SQLite file and force the heuristic router."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")  # force offline heuristic router
    monkeypatch.setenv("RERANKER_ENABLED", "false")  # opt-in; tests enable explicitly
    monkeypatch.setenv("ELIGIBILITY_FILTER_ENABLED", "false")  # opt-in; tests enable explicitly
    monkeypatch.setenv("JOB_VERIFY_APPLY_URLS", "false")  # never hit the network
    monkeypatch.setenv("JOB_CATALOG_PROBE_ENABLED", "false")
    # Keep wide discovery off by default so tests never touch the network; tests
    # that exercise it enable + monkeypatch the fetchers explicitly.
    monkeypatch.setenv("JOB_WIDE_RSS_ENABLED", "false")
    monkeypatch.setenv("JOB_WIDE_DIRECTORY_ENABLED", "false")
    monkeypatch.setenv("JOB_WIDE_SWELIST_ENABLED", "false")
    monkeypatch.setenv("JOB_WIDE_YC_ENABLED", "false")
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "false")
    monkeypatch.setenv("AUTH_LEGACY_USER_ID", "")
    monkeypatch.setenv("AUTH_FAIL_OPEN", "true")
    monkeypatch.setenv("AUTH_ALLOWED_EMAILS", "")
    monkeypatch.setenv("SENTRY_DSN", "")
    monkeypatch.setenv("LLM_MAX_CALLS_PER_USER_PER_DAY", "0")
    monkeypatch.setenv("RESUME_TAILOR_ENABLED", "false")
    monkeypatch.setenv("COVER_LETTER_ENABLED", "false")
    # Autofill/mobile endpoints are token-gated when APPLY_API_TOKEN is set in
    # .env (prod/local). Clear it so suite requests without X-Apply-Token stay
    # open; tests that exercise the gate set the token explicitly.
    monkeypatch.setenv("APPLY_API_TOKEN", "")
    # Neutralize the .env fallback in get_settings so a real key in .env can't
    # pull tests onto the live (paid) API — tests must stay offline.
    monkeypatch.setattr("dotenv.dotenv_values", lambda *a, **k: {})

    # Reset cached settings + router singleton so env changes take effect.
    from app import (
        auth, config, discovery, llm_budget,
        matcher, reminders, router,
    )

    config.get_settings.cache_clear()
    router._router_singleton = None
    reminders._sender_singleton = None  # don't leak a sender across tests
    auth.reset_for_tests()
    discovery.reset_for_tests()
    matcher._llm_client = None  # nor a matcher LLM client/limiter
    matcher._llm_limiter = None
    llm_budget.set_user("")
    from app import catalog
    catalog.reset_cache()
    from app.jobsources import alive
    alive.reset_cache()

    from app.db import init_db

    init_db()
    yield
    os.unlink(path)
