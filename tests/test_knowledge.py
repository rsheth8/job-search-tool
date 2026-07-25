"""Personal knowledge store: what makes a drafted answer specific to *you*.

Covers the store itself, the canned-answer shortcut (a saved answer is reused with
no model call at all), the grounding block that rides along on drafting prompts,
the coverage audit, and the Slack surface for teaching it facts.

Everything here runs offline — the drafting tests stub the model call and assert on
what was *passed* to it, which is the part that matters.
"""
from __future__ import annotations

import pytest

from app import engine, knowledge, router
from app.intents import Intent


# --- the store --------------------------------------------------------------

def test_add_and_list_by_category():
    knowledge.add("u1", "project", "Built a real-time pricing service in Go")
    knowledge.add("u1", "achievement", "Cut p99 latency 40%")
    assert len(knowledge.list_all("u1")) == 2
    assert len(knowledge.list_all("u1", category="project")) == 1
    assert knowledge.list_all("u2") == []          # scoped per user


def test_add_rejects_junk():
    assert knowledge.add("u1", "nonsense", "x") is None      # unknown category
    assert knowledge.add("u1", "project", "   ") is None     # empty text
    # an answer with no question could never be matched back, so it's refused
    assert knowledge.add("u1", "answer", "Because I love it") is None
    assert knowledge.add("u1", "answer", "Because I love it", label="Why us?")


def test_remove():
    item = knowledge.add("u1", "strength", "Systems debugging")
    assert knowledge.remove("u1", item["id"]) is True
    assert knowledge.remove("u1", item["id"]) is False       # already gone
    assert knowledge.list_all("u1") == []


def test_remove_is_scoped_to_the_owner():
    item = knowledge.add("u1", "strength", "Systems debugging")
    assert knowledge.remove("u2", item["id"]) is False
    assert len(knowledge.list_all("u1")) == 1


# --- canned answers: the free path -----------------------------------------

def test_canned_answer_matches_a_reworded_question():
    knowledge.add("u1", "answer", "I want to work on infrastructure at scale.",
                  label="Why do you want to work here?")
    # the real form asks a longer variant of the same question
    assert knowledge.canned_answer(
        "u1", "Why do you want to work at Acme?") == \
        "I want to work on infrastructure at scale."


def test_canned_answer_refuses_an_unrelated_question():
    knowledge.add("u1", "answer", "I want to work on infrastructure at scale.",
                  label="Why do you want to work here?")
    assert knowledge.canned_answer(
        "u1", "Describe a time you handled conflict on a team") is None


def test_canned_answer_with_nothing_saved():
    assert knowledge.canned_answer("u1", "Why do you want to work here?") is None


# --- grounding block --------------------------------------------------------

def test_knowledge_block_is_empty_until_taught():
    assert knowledge.knowledge_block("u1") == ""


def test_knowledge_block_groups_what_it_knows():
    knowledge.add("u1", "project", "Built a real-time pricing service in Go")
    knowledge.add("u1", "achievement", "Cut p99 latency 40%")
    knowledge.add("u1", "preference", "I want a role with real ownership")
    knowledge.add("u1", "answer", "Infrastructure at scale.", label="Why us?")
    block = knowledge.knowledge_block("u1")
    assert "PROJECTS I CAN CITE" in block and "pricing service" in block
    assert "ACHIEVEMENTS" in block and "p99" in block
    assert "WHAT I WANT IN A ROLE" in block
    assert "Why us?" in block


# --- audit ------------------------------------------------------------------

def test_audit_on_an_empty_profile_says_what_to_do():
    report = knowledge.audit("u1")
    assert report["score"] == 0.0
    assert "email" in report["identity_missing"]
    assert any("project" in s.lower() for s in report["suggestions"])


def test_audit_credits_what_is_filled_in():
    from app import applicant

    applicant.set_identity("u1", {
        "first_name": "Rahil", "last_name": "Sheth", "email": "r@example.com",
        "phone": "555-0100", "city": "Chicago", "state": "IL",
    })
    knowledge.add("u1", "project", "Built a pricing service")
    report = knowledge.audit("u1")
    assert report["score"] > 0.3
    assert "email" in report["identity_have"]
    assert "email" not in report["identity_missing"]
    assert report["knowledge_counts"]["project"] == 1
    assert not any("project" in s.lower() for s in report["suggestions"])


# --- it actually reaches the drafter ---------------------------------------

def test_a_saved_answer_is_reused_without_calling_the_model(monkeypatch):
    """The deterministic-first rule: a question you've already answered costs
    nothing to answer again."""
    from app import apply_queue, jobstore, outreach
    from app.jobsources import JobPosting

    posting = jobstore.save_posting("u1", JobPosting(
        source="greenhouse", external_id="1", title="Backend Engineer",
        url="https://x/apply", company="Acme", location="Remote",
        description="Build backend services."), relevance_score=0.8, status="queued")
    apply_queue.stage("u1", posting["id"])

    for q in apply_queue.COMMON_QUESTIONS:
        knowledge.add("u1", "answer", f"canned: {q}",
                      label=q.format(company="Acme", title="Backend Engineer"))

    called = []
    monkeypatch.setattr(outreach, "draft_question_answers",
                        lambda *a, **k: called.append(a) or ["drafted"] * len(a[0]))

    qs = apply_queue.get_questions("u1", posting["id"])
    assert called == [], "every question was canned; the drafter should not run"
    assert all(q["answer"].startswith("canned:") for q in qs)


def test_the_drafter_is_given_the_knowledge_block(monkeypatch):
    from app import apply_queue, jobstore, outreach
    from app.jobsources import JobPosting

    posting = jobstore.save_posting("u1", JobPosting(
        source="greenhouse", external_id="1", title="Backend Engineer",
        url="https://x/apply", company="Acme", location="Remote",
        description="Build backend services."), relevance_score=0.8, status="queued")
    apply_queue.stage("u1", posting["id"])
    knowledge.add("u1", "project", "Built a real-time pricing service in Go")

    seen = {}

    def fake(questions, *a, **kw):
        seen.update(kw)
        return ["drafted"] * len(questions)

    monkeypatch.setattr(outreach, "draft_question_answers", fake)
    apply_queue.get_questions("u1", posting["id"])
    assert "pricing service" in seen.get("knowledge_block", "")


def test_drafting_still_works_with_no_knowledge_at_all(monkeypatch):
    """Fail-open: an empty store must not break packaging."""
    from app import apply_queue, jobstore
    from app.jobsources import JobPosting

    posting = jobstore.save_posting("u1", JobPosting(
        source="greenhouse", external_id="1", title="Backend Engineer",
        url="https://x/apply", company="Acme", location="Remote",
        description="Build backend services."), relevance_score=0.8, status="queued")
    apply_queue.stage("u1", posting["id"])
    qs = apply_queue.get_questions("u1", posting["id"])
    assert len(qs) == len(apply_queue.COMMON_QUESTIONS)
    assert all(q["answer"] for q in qs)          # templates, never blank


# --- the Slack surface ------------------------------------------------------

@pytest.mark.parametrize("text,category", [
    ("remember project: I built a pricing service", "project"),
    ("remember achievement: I cut latency 40%", "achievement"),
    ("remember strength: systems debugging", "strength"),
    ("remember preference: I want real ownership", "preference"),
])
def test_remember_routes_with_an_explicit_category(text, category):
    p = router.HeuristicRouter().parse(text)
    assert p.intent == Intent.REMEMBER
    assert p.message.startswith(f"{category}|")


def test_remember_infers_a_category_when_unlabelled():
    engine.handle_sms("u1", "remember I built a real-time pricing service in Go")
    items = knowledge.list_all("u1")
    assert len(items) == 1
    assert items[0]["category"] == "project"
    assert "pricing service" in items[0]["text"]


def test_remember_keeps_the_users_capitalisation():
    engine.handle_sms("u1", "remember project: I built a Go service for NVIDIA")
    assert "Go service for NVIDIA" in knowledge.list_all("u1")[0]["text"]


def test_remember_an_answer_to_a_named_question():
    reply = engine.handle_sms(
        "u1", 'remember answer to "Why do you want to work here?": '
              'I care about infrastructure at scale.')
    assert "saved your answer" in reply.lower()
    item = knowledge.list_all("u1", category="answer")[0]
    assert item["label"] == "Why do you want to work here?"
    assert item["text"] == "I care about infrastructure at scale."


def test_knowledge_question_reports_what_is_known_and_missing():
    engine.handle_sms("u1", "remember project: I built a pricing service")
    reply = engine.handle_sms("u1", "what do you know about me")
    assert "pricing service" in reply
    assert "missing" in reply.lower()


def test_knowledge_question_when_empty_explains_generic_answers():
    reply = engine.handle_sms("u1", "what do you know about me")
    assert "nothing stored yet" in reply.lower()


def test_remember_does_not_swallow_ordinary_messages():
    """"remember" is a strong verb, but these are other intents."""
    r = router.HeuristicRouter()
    assert r.parse("applied to Stripe").intent == Intent.APPLY
    assert r.parse("remind me to follow up with Acme in 3 days").intent == Intent.REMIND
