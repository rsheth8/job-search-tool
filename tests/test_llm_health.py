"""A broken key or model must be loud, not silent.

Every paid call site fails open to a heuristic on purpose, so a wrong key or a
typo'd model name degrades the whole app with nothing to show for it. These pin
the three things that make that visible: the model id is shape-checked, an
implausible id turns the paid path off instead of failing one call per request,
and /health reports whether calls are actually succeeding.
"""
from __future__ import annotations

import sys
import types

from app import config, llm_health


def _keyed(monkeypatch, model="claude-haiku-4-5"):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_MODEL", model)
    config.get_settings.cache_clear()


def _fake_anthropic(monkeypatch, *, fail=None, reply="ok"):
    calls = []

    class _Block:
        type = "text"

        def __init__(self, t):
            self.text = t

    class _Resp:
        def __init__(self, t):
            self.content = [_Block(t)]

    class _Messages:
        def create(self, **kw):
            calls.append(kw)
            if fail is not None:
                raise fail
            return _Resp(reply)

    class _Client:
        def __init__(self, api_key=None):
            self.messages = _Messages()

    mod = types.ModuleType("anthropic")
    mod.Anthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return calls


# --- model id validation -------------------------------------------------

def test_real_model_ids_are_accepted():
    for good in ("claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-1",
                 "claude-3-5-sonnet-20241022", "claude-opus-5",
                 "claude-haiku-4-5-20251001"):
        assert llm_health.model_looks_valid(good), good


def test_the_aliases_that_silently_broke_everything_are_rejected():
    """'sonnet' is not a model id. This is the value that leaked in from a shell
    env var and disabled every AI feature with only a log line to show it."""
    for bad in ("sonnet", "haiku", "opus", "claude", "", "   ",
                "gpt-4o", "claude-3-5-turbo",
                # Sent verbatim to the API, so case matters: this 404s.
                "Claude-Haiku-4-5"):
        assert not llm_health.model_looks_valid(bad), bad


def test_surrounding_whitespace_is_tolerated_not_fatal(monkeypatch):
    """A stray space in a Fly secret or shell export is a typo, not a new model."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_MODEL", "  claude-haiku-4-5  ")
    config.get_settings.cache_clear()
    s = config.get_settings()
    assert s.anthropic_model == "claude-haiku-4-5"  # stripped at load
    assert s.use_llm_router is True
    assert llm_health.config_problem() is None


def test_a_bad_model_turns_the_paid_path_off(monkeypatch):
    _keyed(monkeypatch, model="sonnet")
    assert config.get_settings().use_llm_router is False
    problem = llm_health.config_problem()
    assert problem is not None
    assert "sonnet" in problem


def test_a_good_model_turns_it_on(monkeypatch):
    _keyed(monkeypatch)
    assert config.get_settings().use_llm_router is True
    assert llm_health.config_problem() is None


def test_a_missing_key_is_not_an_error(monkeypatch):
    """No key is a supported mode, not a misconfiguration."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    config.get_settings.cache_clear()
    assert config.get_settings().use_llm_router is False
    assert "not set" in (llm_health.config_problem() or "")


def test_horizon_is_off_when_the_model_is_bad(monkeypatch):
    from app import horizon

    _keyed(monkeypatch, model="sonnet")
    assert horizon.is_available() is False
    assert horizon.answer("u", "which should I do first") is None


# --- outcome recording ---------------------------------------------------

def test_a_successful_call_is_counted(monkeypatch):
    _keyed(monkeypatch)
    _fake_anthropic(monkeypatch)
    llm_health.client().messages.create(model="claude-haiku-4-5", max_tokens=4,
                                        messages=[{"role": "user", "content": "hi"}])
    snap = llm_health.snapshot()
    assert snap["ok"] == 1 and snap["failed"] == 0


def test_a_failed_call_is_counted_with_the_reason(monkeypatch):
    _keyed(monkeypatch)
    _fake_anthropic(monkeypatch, fail=RuntimeError("not_found_error: model"))
    try:
        llm_health.client().messages.create(model="bogus", max_tokens=4, messages=[])
    except RuntimeError:
        pass
    snap = llm_health.snapshot()
    assert snap["failed"] == 1
    assert "RuntimeError" in snap["last_error"]
    assert "not_found_error" in snap["last_error"]
    assert snap["last_error_model"] == "bogus"


def test_the_wrapper_re_raises_so_call_sites_still_fall_open(monkeypatch):
    """Recording must not swallow the error -- callers rely on catching it."""
    _keyed(monkeypatch)
    _fake_anthropic(monkeypatch, fail=RuntimeError("boom"))
    raised = False
    try:
        llm_health.client().messages.create(model="m", max_tokens=1, messages=[])
    except RuntimeError:
        raised = True
    assert raised


def test_real_call_sites_feed_the_counters(monkeypatch):
    """The point of one shared client: sites get observability for free."""
    from app import outreach

    _keyed(monkeypatch)
    monkeypatch.setenv("LLM_MAX_CALLS_PER_USER_PER_DAY", "0")
    config.get_settings.cache_clear()
    _fake_anthropic(monkeypatch, reply='{"answers":[{"id":0,"answer":"Yes."}]}')
    outreach.draft_question_answers(["Why us?"], "Acme", "Engineer", "")
    assert llm_health.snapshot()["ok"] == 1


# --- probe ---------------------------------------------------------------

def test_probe_reports_config_problems_without_calling(monkeypatch):
    _keyed(monkeypatch, model="sonnet")
    calls = _fake_anthropic(monkeypatch)
    got = llm_health.probe()
    assert got["ok"] is False and got["reason"] == "config"
    assert calls == [], "probe spent a call on a config it knew was broken"


def test_probe_succeeds_against_a_working_key(monkeypatch):
    _keyed(monkeypatch)
    _fake_anthropic(monkeypatch, reply="ok")
    got = llm_health.probe()
    assert got["ok"] is True
    assert got["model"] == "claude-haiku-4-5"


def test_probe_reports_a_revoked_key(monkeypatch):
    """Shape validation can't catch this; only a real call can."""
    _keyed(monkeypatch)
    _fake_anthropic(monkeypatch, fail=RuntimeError("authentication_error: invalid x-api-key"))
    got = llm_health.probe()
    assert got["ok"] is False and got["reason"] == "call_failed"
    assert "authentication_error" in got["detail"]


# --- /health -------------------------------------------------------------

def test_health_reports_llm_state(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    _keyed(monkeypatch)
    body = TestClient(app).get("/health").json()
    assert body["llm"]["configured"] is True
    assert body["llm"]["model_valid"] is True
    assert body["llm"]["problem"] is None
    assert body["llm"]["caps"]["chat"] == config.get_settings().llm_cap_chat
    assert body["beta"]["llm_ready"] is True
    # Horizon now answers unparseable turns, so a bare "heuristic" was wrong.
    assert body["chat_router"] == "heuristic+horizon"


def test_health_goes_degraded_on_a_bad_model(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    _keyed(monkeypatch, model="sonnet")
    body = TestClient(app).get("/health").json()
    assert body["status"] == "degraded"
    assert body["llm"]["model_valid"] is False
    assert body["beta"]["llm_ready"] is False
    assert "sonnet" in body["llm"]["problem"]
    assert body["chat_router"] == "heuristic"


def test_health_without_a_key_is_not_degraded(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    config.get_settings.cache_clear()
    body = TestClient(app).get("/health").json()
    assert body["status"] == "ok"
    assert body["llm"]["configured"] is False
    assert body["chat_router"] == "heuristic"


def test_health_llm_probe_endpoint_is_gated(monkeypatch):
    """It spends a real call, so it must not be open to the internet."""
    from fastapi.testclient import TestClient

    from app.main import app

    _keyed(monkeypatch)
    monkeypatch.setenv("APPLY_API_TOKEN", "secret-token")
    monkeypatch.setenv("AUTH_FAIL_OPEN", "false")
    config.get_settings.cache_clear()
    calls = _fake_anthropic(monkeypatch)
    c = TestClient(app)

    assert c.get("/health/llm").status_code == 401
    assert calls == [], "an unauthenticated request spent a paid call"

    ok = c.get("/health/llm", headers={"X-Apply-Token": "secret-token"})
    assert ok.status_code == 200
    assert ok.json()["probe"]["ok"] is True


# --- optional dependencies ----------------------------------------------
# Same disease as a bad model id: an enabled feature whose dependency is absent
# logs and skips, so it looks fine from outside.

def test_health_reports_optional_dependencies(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    body = TestClient(app).get("/health").json()
    deps = body["dependencies"]
    for key in ("pypdf", "tectonic", "pyjwt", "cryptography", "missing"):
        assert key in deps


def test_missing_pypdf_is_flagged_when_resumes_are_on(monkeypatch):
    from app import main

    monkeypatch.setenv("RESUME_TAILOR_ENABLED", "true")
    config.get_settings.cache_clear()
    monkeypatch.setattr("importlib.util.find_spec",
                        lambda name, *a: None if name == "pypdf" else object())
    report = main._dependency_report()
    assert report["pypdf"] is False
    assert any("pypdf" in m for m in report["missing"])


def test_nothing_is_flagged_when_the_feature_is_off(monkeypatch):
    """A missing dep for a disabled feature is not a problem."""
    from app import main

    monkeypatch.setenv("RESUME_TAILOR_ENABLED", "false")
    monkeypatch.setenv("COVER_LETTER_ENABLED", "false")
    monkeypatch.setenv("PUSH_ENABLED", "false")
    config.get_settings.cache_clear()
    monkeypatch.setattr("importlib.util.find_spec", lambda name, *a: None)
    assert main._dependency_report()["missing"] == []


def test_push_without_signing_libs_is_flagged(monkeypatch):
    from app import main

    monkeypatch.setenv("PUSH_ENABLED", "true")
    monkeypatch.setenv("RESUME_TAILOR_ENABLED", "false")
    monkeypatch.setenv("COVER_LETTER_ENABLED", "false")
    config.get_settings.cache_clear()
    monkeypatch.setattr("importlib.util.find_spec",
                        lambda name, *a: None if name in ("jwt", "cryptography") else object())
    report = main._dependency_report()
    assert any("pyjwt" in m for m in report["missing"])
