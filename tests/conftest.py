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
    # Pin the model: it's read from the environment, so a developer shell with
    # ANTHROPIC_MODEL set leaked into the suite. Tests that enable the LLM need a
    # plausible id, since use_llm_router now rejects aliases like "sonnet".
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
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
    monkeypatch.setenv("JOB_WIDE_WORKDAY_ENABLED", "false")
    monkeypatch.setenv("JOB_WIDE_AMAZON_ENABLED", "false")
    monkeypatch.setenv("JOB_WIDE_NETFLIX_ENABLED", "false")
    monkeypatch.setenv("JOB_WIDE_USAJOBS_ENABLED", "false")
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
    # ...and stop pydantic-settings reading .env itself, which is a separate
    # path from dotenv_values above. The explicit setenv calls are a whitelist:
    # every setting a developer might have in .env has to be remembered here, and
    # APNS_USE_SANDBOX was not, so `test_production_is_the_default_host` failed
    # on a checkout that has a .env and passed everywhere else — including CI,
    # which has none. Cutting the file out entirely is what makes the suite
    # depend on its own fixtures rather than on whose machine it runs on.
    from app.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)

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
    from app import llm_health
    llm_health.reset_for_tests()  # per-process counters must not leak across tests
    from app import catalog
    catalog.reset_cache()
    from app.jobsources import workday as workday_src
    workday_src.reset_cache()
    from app.jobsources import alive
    alive.reset_cache()

    from app.db import init_db

    init_db()
    yield
    os.unlink(path)
