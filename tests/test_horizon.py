"""Horizon's grounded answer: only takes lost turns, and only with real facts."""
from __future__ import annotations

import sys
import types

from app import config, engine, horizon, jobstore, llm_budget, profile, store


# --- fake Anthropic client -------------------------------------------------

class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


def _install_fake_anthropic(monkeypatch, reply="Start with Databricks.", sink=None):
    """Capture the exact request Horizon would send, without a network call."""
    calls = sink if sink is not None else []

    class _Messages:
        def create(self, **kw):
            calls.append(kw)
            return _Resp(reply)

    class _Client:
        def __init__(self, api_key=None):
            self.api_key = api_key
            self.messages = _Messages()

    mod = types.ModuleType("anthropic")
    mod.Anthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return calls


def _keyed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    config.get_settings.cache_clear()


def _seed(uid="u"):
    profile.set_profile(uid, roles="software engineer",
                        keywords="python, kubernetes, aws",
                        locations="Chicago, IL, Remote")
    jobstore.save_posting(uid, _posting("1", "Senior Software Engineer", "Databricks"),
                          relevance_score=0.92, status="alerted")
    jobstore.save_posting(uid, _posting("2", "Backend Engineer", "Figma"),
                          relevance_score=0.71, status="queued")
    store.create_application(uid, "Stripe", "SWE", status="Phone screen")


def _posting(ext, title, company):
    from app.jobsources import JobPosting

    return JobPosting("greenhouse", ext, title, f"https://x/{ext}",
                      company=company, location="Remote",
                      description="python kubernetes aws")


# --- no key: behaviour is unchanged ---------------------------------------

def test_no_key_means_no_horizon_and_the_old_reply_stands():
    assert horizon.is_available() is False
    assert horizon.answer("u", "which of these should I do first?") is None
    reply = engine.handle_sms("u", "which of these should I do first?")
    assert "I didn't fully understand that" in reply


def test_commands_never_reach_horizon(monkeypatch):
    """The heuristic router keeps every turn it can parse -- no paid call."""
    _keyed(monkeypatch)
    calls = _install_fake_anthropic(monkeypatch)
    _seed()
    for text in ("applied to stripe", "show new jobs", "commands", "stats"):
        engine.handle_sms("u", text)
    assert calls == [], f"a parseable command escalated to Claude: {calls}"


# --- keyed: the lost turn gets answered ----------------------------------

def test_unknown_turn_is_answered_by_horizon(monkeypatch):
    _keyed(monkeypatch)
    _install_fake_anthropic(monkeypatch, reply="Databricks first — it's your strongest match.")
    _seed()
    reply = engine.handle_sms("u", "honestly which of these is worth my saturday")
    assert reply == "Databricks first — it's your strongest match."
    assert "I didn't fully understand" not in reply


def test_prompt_carries_their_real_profile_matches_and_applications(monkeypatch):
    _keyed(monkeypatch)
    calls = _install_fake_anthropic(monkeypatch)
    _seed()
    engine.handle_sms("u", "why am I seeing what I'm seeing")

    assert len(calls) == 1
    sent = calls[0]
    body = sent["messages"][0]["content"]
    # Real profile
    assert "software engineer" in body
    assert "kubernetes" in body
    # Real matches, with real scores
    assert "Senior Software Engineer" in body and "Databricks" in body
    assert "92%" in body
    assert "Backend Engineer" in body and "Figma" in body
    # Real applications
    assert "Stripe" in body and "Phone screen" in body
    # The question itself
    assert "why am I seeing what I'm seeing" in body
    # Grounding rules that stop it inventing the app's behaviour
    assert "never submits" in sent["system"].lower()
    assert "only" in sent["system"].lower()


def test_answer_is_capped_and_uses_the_configured_model(monkeypatch):
    _keyed(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
    config.get_settings.cache_clear()
    calls = _install_fake_anthropic(monkeypatch)
    _seed()
    engine.handle_sms("u", "honestly what do you reckon about all this")
    assert calls[0]["model"] == "claude-haiku-4-5"
    assert calls[0]["max_tokens"] == horizon.MAX_TOKENS


# --- budget ---------------------------------------------------------------

def test_horizon_is_charged_to_the_chat_slice(monkeypatch):
    _keyed(monkeypatch)
    monkeypatch.setenv("LLM_MAX_CALLS_PER_USER_PER_DAY", "50")
    monkeypatch.setenv("LLM_CAP_CHAT", "2")
    config.get_settings.cache_clear()
    _install_fake_anthropic(monkeypatch)
    _seed()

    for _ in range(2):
        assert horizon.answer("u", "what now") is not None
    # Slice spent: falls back rather than spending discovery's budget.
    assert horizon.answer("u", "what now") is None
    assert llm_budget.calls_today("u", feature="chat") == 2
    # Discovery's slice is untouched.
    assert llm_budget.consume("u", feature="discovery") is True


def test_capped_horizon_still_returns_the_written_fallback(monkeypatch):
    _keyed(monkeypatch)
    monkeypatch.setenv("LLM_CAP_CHAT", "0")
    monkeypatch.setenv("LLM_MAX_CALLS_PER_USER_PER_DAY", "1")
    config.get_settings.cache_clear()
    _install_fake_anthropic(monkeypatch)
    _seed()
    llm_budget.consume("u")  # spend the only global slot
    reply = engine.handle_sms("u", "hmm which one of these actually matters most")
    assert "I didn't fully understand that" in reply


# --- failure modes --------------------------------------------------------

def test_a_dead_api_does_not_eat_the_turn(monkeypatch):
    _keyed(monkeypatch)

    class _Boom:
        def __init__(self, api_key=None):
            self.messages = self

        def create(self, **kw):
            raise RuntimeError("api down")

    mod = types.ModuleType("anthropic")
    mod.Anthropic = _Boom
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    _seed()
    reply = engine.handle_sms("u", "so what do you reckon")
    assert "I didn't fully understand that" in reply


def test_empty_model_reply_falls_back(monkeypatch):
    _keyed(monkeypatch)
    _install_fake_anthropic(monkeypatch, reply="   ")
    _seed()
    reply = engine.handle_sms("u", "hmm what do you think")
    assert "I didn't fully understand that" in reply


def test_context_survives_a_brand_new_user(monkeypatch):
    """No profile, no matches, no applications -- still a usable prompt."""
    _keyed(monkeypatch)
    block = horizon.context_block("nobody")
    assert block.strip()
    assert "not set up yet" in block or "no profile data yet" in block


def test_company_disambiguation_still_beats_horizon(monkeypatch):
    """A bare company name has a better heuristic answer than a model guess."""
    _keyed(monkeypatch)
    calls = _install_fake_anthropic(monkeypatch)
    _seed()
    reply = engine.handle_sms("u", "spotify")
    assert "1) apply" in reply
    assert calls == []


# --- through the real HTTP surface the iOS Ask tab uses -------------------

def test_ask_tab_gets_horizon_over_post_chat(monkeypatch):
    """POST /chat is what ChatView calls; no iOS change is needed for Horizon."""
    from fastapi.testclient import TestClient

    from app.main import app

    _keyed(monkeypatch)
    calls = _install_fake_anthropic(monkeypatch, reply="Databricks first.")
    _seed("usr_ask")
    from app import auth
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    config.get_settings.cache_clear()
    token = auth.sign_in_dev(user_id="usr_ask")["token"]

    c = TestClient(app)
    r = c.post("/chat", json={"text": "honestly which of these is worth my saturday"},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "Databricks first."
    assert len(calls) == 1
    assert "Databricks" in calls[0]["messages"][0]["content"]


def test_agent_low_confidence_falls_through_to_horizon(monkeypatch):
    """On-device classification that gives up still reaches the grounded answer."""
    from fastapi.testclient import TestClient

    from app.main import app

    _keyed(monkeypatch)
    calls = _install_fake_anthropic(monkeypatch, reply="Start with Databricks.")
    _seed("usr_agent")
    from app import auth
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    config.get_settings.cache_clear()
    token = auth.sign_in_dev(user_id="usr_agent")["token"]

    c = TestClient(app)
    r = c.post("/agent", json={"action": "unknown", "slots": {},
                               "raw_text": "hmm which of these actually matters"},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "Start with Databricks."
    assert len(calls) == 1
