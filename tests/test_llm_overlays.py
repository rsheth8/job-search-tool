"""The already-built Claude overlays: do they fire, and do they fix anything?

Seven modules call Claude, all gated on a key nobody has locally, so these
paths ship untested. These stub the client to prove the wiring works end to end
-- the request is shaped right, the response merges correctly, the daily slice
is charged, and a dead API falls open to the heuristic instead of the turn dying.

What they cannot check is answer *quality*; that needs a real key.
"""
from __future__ import annotations

import json
import sys
import types

from app import config, llm_budget, outreach, profile_import


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


def _fake_anthropic(monkeypatch, payload, calls=None):
    """Install a stub whose reply is ``payload`` (dict -> JSON, str -> as-is)."""
    seen = calls if calls is not None else []
    body = json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)

    class _Messages:
        def create(self, **kw):
            seen.append(kw)
            return _Resp(body)

    class _Client:
        def __init__(self, api_key=None):
            self.messages = _Messages()

    mod = types.ModuleType("anthropic")
    mod.Anthropic = _Client
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    return seen


def _keyed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("LLM_MAX_CALLS_PER_USER_PER_DAY", "0")
    config.get_settings.cache_clear()


# --- resume overlay (profile_import._llm_parse) ---------------------------

# The header layout that made the heuristic truncate the school last pass:
# the regex stops at the lowercase "at" in "University of Illinois at Urbana".
RESUME = """Rahil Sheth
Chicago, IL | rahil.sheth@example.com | (312) 555-0147

PROFESSIONAL EXPERIENCE
Acme Corp - Senior Platform Engineer  Mar 2022 - Present
Chicago, IL
6 years building Python backends.

EDUCATION
University of Illinois at Urbana-Champaign  2019
B.S. Computer Science

SKILLS
Python, FastAPI, PostgreSQL, Docker, Kubernetes, AWS
"""


def test_heuristic_keeps_the_whole_school_name():
    """The name used to stop at the lowercase "at" -- no key needed to get it right."""
    got = profile_import.parse_document(RESUME)
    assert got["identity"].get("school") == "University of Illinois at Urbana-Champaign"


def test_the_sanitizer_no_longer_truncates_the_overlay(monkeypatch):
    """_clean_school runs on the LLM's output too, so it used to undo a correct
    paid extraction -- the overlay could never fix this field."""
    _keyed(monkeypatch)
    _fake_anthropic(monkeypatch, {
        "identity": {"school": "University of North Carolina at Chapel Hill",
                     "first_name": "Rahil", "last_name": "Sheth"},
        "profile": {},
        "knowledge": [],
    })
    got = profile_import.parse_document(RESUME)
    assert got["identity"]["school"] == "University of North Carolina at Chapel Hill"


def test_a_degree_still_never_lands_in_the_school_name():
    """The guard that made loosening the pattern safe."""
    from app.profile_import import _clean_school

    assert _clean_school("University of Minnesota B.S. Computer Science") \
        == "University of Minnesota"
    assert _clean_school("University of Texas at Austin M.S. Data Science") \
        == "University of Texas at Austin"
    assert _clean_school("University") == ""
    assert _clean_school("LEADERSHIP") == ""


def test_overlay_cannot_blank_a_field_the_heuristic_found(monkeypatch):
    """Empty overlay values are ignored, so a lazy model can't erase good data."""
    _keyed(monkeypatch)
    _fake_anthropic(monkeypatch, {
        "identity": {"school": "", "first_name": ""},
        "profile": {"roles": ""},
        "knowledge": [],
    })
    got = profile_import.parse_document(RESUME)
    assert got["identity"].get("school") == "University of Illinois at Urbana-Champaign"
    assert got["identity"].get("first_name") == "Rahil"


def test_overlay_is_charged_to_the_parse_slice(monkeypatch):
    _keyed(monkeypatch)
    monkeypatch.setenv("LLM_MAX_CALLS_PER_USER_PER_DAY", "50")
    monkeypatch.setenv("LLM_CAP_PARSE", "1")
    config.get_settings.cache_clear()
    _fake_anthropic(monkeypatch, {"identity": {"school": "Full Name University"},
                                  "profile": {}, "knowledge": []})
    llm_budget.set_user("u")

    assert profile_import.parse_document(RESUME)["identity"]["school"] == "Full Name University"
    # Slice spent: back to the heuristic, and the turn still works.
    assert profile_import.parse_document(RESUME)["identity"]["school"] \
        == "University of Illinois at Urbana-Champaign"
    assert llm_budget.calls_today("u", feature="parse") == 1


def test_bad_overlay_json_falls_back_to_the_heuristic(monkeypatch):
    _keyed(monkeypatch)
    _fake_anthropic(monkeypatch, "not json at all")
    got = profile_import.parse_document(RESUME)
    assert got["identity"].get("school") == "University of Illinois at Urbana-Champaign"
    assert got["identity"].get("first_name") == "Rahil"


def test_dead_api_does_not_break_import(monkeypatch):
    _keyed(monkeypatch)

    class _Boom:
        def __init__(self, api_key=None):
            self.messages = self

        def create(self, **kw):
            raise RuntimeError("api down")

    mod = types.ModuleType("anthropic")
    mod.Anthropic = _Boom
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    got = profile_import.parse_document(RESUME)
    assert got["identity"].get("first_name") == "Rahil"


# --- free-text application answers (outreach) ----------------------------

QUESTIONS = [
    "Why do you want to work at Acme?",
    "Describe a hard technical problem you solved.",
    "What are you looking for in your next role?",
]


def test_all_questions_are_answered_in_one_batched_call(monkeypatch):
    _keyed(monkeypatch)
    calls = _fake_anthropic(monkeypatch, {"answers": [
        {"id": 0, "answer": "Acme's platform work matches what I do."},
        {"id": 1, "answer": "I cut a 40-minute batch job to 90 seconds."},
        {"id": 2, "answer": "Deeper ownership of backend systems."},
    ]})
    out = outreach.draft_question_answers(QUESTIONS, "Acme", "Senior Platform Engineer",
                                         "python kubernetes aws")
    assert len(out) == 3
    assert out[1] == "I cut a 40-minute batch job to 90 seconds."
    # One call for three questions -- cost scales per posting, not per question.
    assert len(calls) == 1
    body = calls[0]["messages"][0]["content"]
    assert "Acme" in body and "Senior Platform Engineer" in body
    for q in QUESTIONS:
        assert q in body


def test_a_missing_answer_id_falls_back_per_question(monkeypatch):
    """A partial model reply degrades one answer, not the whole set."""
    _keyed(monkeypatch)
    _fake_anthropic(monkeypatch, {"answers": [{"id": 0, "answer": "Real answer."}]})
    out = outreach.draft_question_answers(QUESTIONS, "Acme", "Engineer", "")
    assert out[0] == "Real answer."
    assert "genuinely excited" in out[1]  # template
    assert len(out) == 3


def test_answers_are_charged_to_the_draft_slice(monkeypatch):
    _keyed(monkeypatch)
    monkeypatch.setenv("LLM_MAX_CALLS_PER_USER_PER_DAY", "50")
    monkeypatch.setenv("LLM_CAP_DRAFT", "1")
    config.get_settings.cache_clear()
    _fake_anthropic(monkeypatch, {"answers": [{"id": 0, "answer": "Drafted."}]})
    llm_budget.set_user("u")

    assert outreach.draft_question_answers(["Why us?"], "Acme", "Eng", "")[0] == "Drafted."
    # Slice spent: templates, not an exception.
    second = outreach.draft_question_answers(["Why us?"], "Acme", "Eng", "")
    assert "genuinely excited" in second[0]
    assert llm_budget.calls_today("u", feature="draft") == 1


def test_dead_api_yields_templates_not_an_error(monkeypatch):
    _keyed(monkeypatch)

    class _Boom:
        def __init__(self, api_key=None):
            self.messages = self

        def create(self, **kw):
            raise RuntimeError("api down")

    mod = types.ModuleType("anthropic")
    mod.Anthropic = _Boom
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    out = outreach.draft_question_answers(QUESTIONS, "Acme", "Engineer", "")
    assert len(out) == 3
    assert all("genuinely excited" in a for a in out)


def test_already_answered_questions_never_reach_the_model(monkeypatch):
    """knowledge.canned_answer is deterministic reuse: same answer, no cost."""
    from app import knowledge

    _keyed(monkeypatch)
    calls = _fake_anthropic(monkeypatch, {"answers": []})
    q = "Why do you want to work at Acme?"
    knowledge.add("u", "answer", "I want to work at Acme because of the platform team.",
                  label=q)
    reused = knowledge.canned_answer("u", q)
    assert reused is not None
    assert calls == [], "a saved answer still triggered a paid call"


# --- the generated bio (onboarding._about_template) ----------------------

def test_an_experienced_candidate_is_not_described_as_studying():
    """A 2019 graduate introduced themselves as a student on every application."""
    from app.onboarding import _about_template

    about = _about_template(
        {"first_name": "Rahil", "school": "University of Illinois at Urbana-Champaign",
         "degree": "B.S.", "discipline": "Computer Science", "grad_year": "2019"},
        {"roles": "software engineer"},
    )
    assert "studying" not in about
    assert "B.S. in Computer Science from University of Illinois at Urbana-Champaign" in about


def test_a_future_graduate_is_still_studying():
    from app.onboarding import _about_template

    about = _about_template(
        {"first_name": "Ana", "school": "University of Minnesota", "degree": "B.S.",
         "discipline": "Computer Science", "grad_year": "2027"},
        {"roles": "software intern"},
    )
    assert "studying B.S. in Computer Science at University of Minnesota" in about


def test_an_unknown_grad_year_keeps_the_cautious_wording():
    from app.onboarding import _about_template

    about = _about_template(
        {"first_name": "Kai", "school": "Northwestern University", "degree": "B.S.",
         "discipline": "CS"},
        {"roles": "backend engineer"},
    )
    assert "studying" in about


def test_school_without_a_degree_reads_as_alum_once_graduated():
    from app.onboarding import _about_template

    about = _about_template(
        {"first_name": "Lee", "school": "Georgia Institute of Technology",
         "grad_year": "2015"},
        {"roles": "platform engineer"},
    )
    assert "Georgia Institute of Technology alum" in about
