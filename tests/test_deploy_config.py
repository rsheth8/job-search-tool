"""fly.toml said one thing; the app did another.

Fly injects ``[env]`` and ``fly secrets`` into the same environment and secrets
win, so a secret set once outranks every later edit to the file. That is how a
release adding four job sources shipped them switched off: the code deployed,
the boards file deployed, ``JOB_SOURCES_ENABLED`` was a secret holding a stale
list, and both the deploy and /health were green.

Nothing anywhere compared the two. Now /health does, and the preflight blocks
on it.
"""
from __future__ import annotations

import pytest

from app import deploy_config

FLY = {"FLY_APP_NAME": "job-search-tool"}


@pytest.fixture(autouse=True)
def _fresh():
    deploy_config.reset_cache()
    yield
    deploy_config.reset_cache()


def test_it_reads_the_env_table_that_shipped_with_the_image():
    declared = deploy_config.declared_env()
    assert declared, "fly.toml [env] didn't parse"
    assert declared["JOB_POLL_SECONDS"] == "600"
    assert "workday" in declared["JOB_SOURCES_ENABLED"]


def test_a_shadowed_key_is_reported_by_name():
    env = FLY | {"JOB_SOURCES_ENABLED": "greenhouse,lever"}
    assert deploy_config.shadowed_keys(env) == ["JOB_SOURCES_ENABLED"]


def test_the_real_incident_reproduces():
    """The stale secret, verbatim. This is the case the module exists for."""
    env = FLY | {"JOB_SOURCES_ENABLED":
                 "greenhouse,lever,ashby,rss,directory,aggregator"}
    assert "JOB_SOURCES_ENABLED" in deploy_config.shadowed_keys(env)


def test_agreement_is_silent():
    declared = deploy_config.declared_env()
    assert deploy_config.shadowed_keys(FLY | declared) == []


def test_whitespace_is_not_a_difference():
    declared = deploy_config.declared_env()
    env = FLY | {"JOB_POLL_SECONDS": f"  {declared['JOB_POLL_SECONDS']} "}
    assert deploy_config.shadowed_keys(env) == []


def test_a_key_absent_from_the_environment_is_not_shadowed():
    """Only a *different* value means something is overriding the file."""
    assert deploy_config.shadowed_keys(dict(FLY)) == []


def test_it_says_nothing_off_fly():
    """A developer's shell disagreeing with fly.toml is normal, not a finding."""
    assert deploy_config.shadowed_keys({"JOB_SOURCES_ENABLED": "nonsense"}) == []


def test_it_never_reports_a_value():
    """/health is public and the shadowing value is usually a secret."""
    secret = "sk-do-not-print-me"
    out = deploy_config.shadowed_keys(FLY | {"JOB_SOURCES_ENABLED": secret})
    assert out == ["JOB_SOURCES_ENABLED"]
    assert secret not in repr(out)


def test_health_carries_the_field(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    body = TestClient(app).get("/health").json()
    assert body["config_shadowed"] == [], "local run should be quiet"


def test_the_preflight_blocks_on_drift():
    from scripts.beta_preflight import evaluate

    clean = evaluate({"config_shadowed": []})
    dirty = evaluate({"config_shadowed": ["JOB_SOURCES_ENABLED"]})
    line = "fly.toml matches the live config"
    assert any(line in row and "FAIL" in row.upper()
               for row in _rows(dirty)), _rows(dirty)
    assert not any(line in row and "FAIL" in row.upper() for row in _rows(clean))
    assert dirty.blocking_failures == clean.blocking_failures + 1


def _rows(report) -> list[str]:
    return [" ".join(str(p) for p in row) for row in report.rows]
