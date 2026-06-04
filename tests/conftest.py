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
    monkeypatch.setenv("APOLLO_API_KEY", "")  # never hit live Apollo from .env
    # Neutralize live Slack tokens from .env so the webhook tests post unsigned
    # (no real signing secret) and no test ever calls the Slack Web API. Tests
    # that exercise signing/outbound set these explicitly via monkeypatch.
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "")
    # Neutralize the .env fallback in get_settings so a real key in .env can't
    # pull tests onto the live (paid) API — tests must stay offline.
    monkeypatch.setattr("dotenv.dotenv_values", lambda *a, **k: {})

    # Reset cached settings + router singleton so env changes take effect.
    from app import apollo, config, reminders, router

    config.get_settings.cache_clear()
    router._router_singleton = None
    reminders._sender_singleton = None  # don't leak a sender across tests
    apollo._client_singleton = None  # nor an Apollo client
    apollo._last_discovery_issue = None
    apollo.reset_for_tests()

    from app.db import init_db

    init_db()
    yield
    os.unlink(path)
