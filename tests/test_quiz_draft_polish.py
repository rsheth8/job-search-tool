"""Horizon drafting the quiz's written answers.

"Ask Horizon" left *strength* and *what you want in a role* blank every single
time. Not a model failure: the request asked for six fields, described two of
them, and showed the model three. These pin the contract so the schema, the
prompt and the copy-back cannot drift apart again.
"""
from __future__ import annotations

import json

import pytest

from app import onboarding


def test_every_drafted_field_is_in_the_schema():
    assert set(onboarding._POLISH_FIELDS) == set(
        onboarding._POLISH_SCHEMA["properties"])


def test_strength_and_preference_are_fields_horizon_drafts():
    """The two that were silently never asked for."""
    assert "strength" in onboarding._POLISH_FIELDS
    assert "preference" in onboarding._POLISH_FIELDS


class _Recorder:
    """Stands in for the Anthropic client and keeps the request."""

    def __init__(self, reply: dict):
        self.reply = reply
        self.system = ""
        self.user = ""
        self.messages = self

    def create(self, **kwargs):
        self.system = kwargs["system"]
        self.user = kwargs["messages"][0]["content"]
        block = type("B", (), {"type": "text", "text": json.dumps(self.reply)})
        return type("R", (), {"content": [block()]})()


@pytest.fixture
def recorded(monkeypatch):
    """Run _llm_polish_quiz against a fake client and hand back the request."""
    def run(reply: dict, draft: dict):
        rec = _Recorder(reply)
        from app import llm_budget, llm_health
        from app.config import get_settings

        s = get_settings()
        monkeypatch.setattr(type(s), "use_llm_router", property(lambda self: True))
        monkeypatch.setattr(llm_budget, "set_user", lambda *a, **k: None)
        monkeypatch.setattr(llm_budget, "consume", lambda *a, **k: True)
        monkeypatch.setattr(llm_health, "client", lambda *a, **k: rec)
        out = onboarding._llm_polish_quiz(
            "usr_x",
            {"first_name": "Rahil", "current_title": "Software Engineer Intern",
             "current_company": "Stripe", "years_experience": "1"},
            {"roles": "backend engineer", "keywords": "Go, Kubernetes",
             "locations": "Chicago"},
            [{"category": "project", "text": "Built a reconciliation service in Go."}],
            draft,
        )
        return rec, out
    return run


def test_the_request_carries_every_field_not_three_of_them(recorded):
    """The actual bug: strength and preference were never in the context."""
    rec, _ = recorded({}, {k: "" for k in onboarding._POLISH_FIELDS})
    sent = json.loads(rec.user.split("Current draft: ", 1)[1])
    assert set(sent) == set(onboarding._POLISH_FIELDS)


def test_the_prompt_says_what_strength_and_preference_mean(recorded):
    """A field named in the schema and nowhere in the instructions comes back
    empty, which is exactly what happened."""
    rec, _ = recorded({}, {k: "" for k in onboarding._POLISH_FIELDS})
    assert "strength:" in rec.system
    assert "preference:" in rec.system


def test_the_resume_derived_work_is_offered_as_grounding(recorded):
    """Strength and preference are inferred from the pattern of the history,
    so the history has to be in the request."""
    rec, _ = recorded({}, {k: "" for k in onboarding._POLISH_FIELDS})
    assert "Stripe" in rec.user
    assert "Kubernetes" in rec.user


def test_a_drafted_strength_reaches_the_quiz(recorded, monkeypatch):
    """End of the pipe: what Horizon returns has to land on the draft."""
    from app import applicant, db, knowledge, profile

    db.init_db()
    uid = "usr_polish"
    profile.set_profile(uid, roles="backend engineer", keywords="Go")
    applicant.set_identity(uid, {"first_name": "Rahil"})
    knowledge.add(uid, "project", "Built a reconciliation service in Go.")

    reply = {"strength": "I like unglamorous backend glue.",
             "preference": "Small teams, real systems."}
    rec = _Recorder(reply)
    from app import llm_budget, llm_health
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(type(s), "use_llm_router", property(lambda self: True))
    monkeypatch.setattr(llm_budget, "set_user", lambda *a, **k: None)
    monkeypatch.setattr(llm_budget, "consume", lambda *a, **k: True)
    monkeypatch.setattr(llm_health, "client", lambda *a, **k: rec)

    draft = onboarding.quiz_draft(uid, polish=True)
    assert draft["strength"] == "I like unglamorous backend glue."
    assert draft["preference"] == "Small teams, real systems."


def test_polish_never_blanks_an_answer_already_there(recorded, monkeypatch):
    """An empty string from the model means "nothing to add", not "delete it"."""
    from app import db, knowledge, profile

    db.init_db()
    uid = "usr_polish_keep"
    profile.set_profile(uid, roles="backend engineer")
    knowledge.add(uid, "strength", "My own words about what I am good at.")

    rec = _Recorder({"strength": ""})
    from app import llm_budget, llm_health
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(type(s), "use_llm_router", property(lambda self: True))
    monkeypatch.setattr(llm_budget, "set_user", lambda *a, **k: None)
    monkeypatch.setattr(llm_budget, "consume", lambda *a, **k: True)
    monkeypatch.setattr(llm_health, "client", lambda *a, **k: rec)

    draft = onboarding.quiz_draft(uid, polish=True)
    assert draft["strength"] == "My own words about what I am good at."
