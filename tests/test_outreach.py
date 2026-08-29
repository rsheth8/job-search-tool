"""Application answer drafting (offline templates)."""
from __future__ import annotations

from app import outreach


def test_draft_application_answers_template():
    msg = outreach.draft_application_answers("Stripe", "SWE", None)
    assert "Stripe" in msg
    assert "SWE" in msg


def test_answer_application_question_template():
    ans = outreach.answer_application_question(
        "Why do you want to work here?", "Stripe", "SWE", None,
    )
    assert "Stripe" in ans
    assert "Why do you want to work here?" in ans


def test_draft_question_answers_returns_one_per_question():
    qs = ["Why us?", "Tell us about a project"]
    answers = outreach.draft_question_answers(qs, "Stripe", "SWE", None)
    assert len(answers) == 2
    assert all(a for a in answers)
