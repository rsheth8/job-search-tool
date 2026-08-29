"""Application answer drafting for the apply queue.

Turn a JD + profile into per-question answers (and a "why I'm a fit" blurb).
Claude when keyed; otherwise clean templates. Drafting never hard-fails.
"""
from __future__ import annotations

import json
import logging

from .config import get_settings

logger = logging.getLogger("outreach")

_QA_SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "answer": {"type": "string"}},
                "required": ["id", "answer"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["answers"],
    "additionalProperties": False,
}


def draft_question_answers(
    questions: list[str], company: str, title: str, description: str | None,
    profile_row=None, *, identity_block: str = "", knowledge_block: str = "",
) -> list[str]:
    """Draft a tailored answer for EACH question in one batched Haiku call."""
    if not questions:
        return []
    background = ""
    if profile_row is not None:
        from .profile import profile_text

        background = profile_text(profile_row)
    if get_settings().use_llm_router:
        try:
            return _draft_question_answers_via_claude(
                questions, company, title, description, background, identity_block,
                knowledge_block,
            )
        except Exception:  # network/auth/parse — fall back, never block
            logger.exception("Claude batched answers failed; using templates")
    return [_answer_question_template(q, company, title) for q in questions]


def _draft_question_answers_via_claude(
    questions: list[str], company: str, title: str, description: str | None,
    background: str, identity_block: str, knowledge_block: str = "",
) -> list[str]:
    import anthropic

    from . import llm_budget

    if not llm_budget.consume():
        raise RuntimeError("llm user daily cap")
    s = get_settings()
    client = anthropic.Anthropic(api_key=s.anthropic_api_key)
    desc = (description or "").strip()[:1100]
    listing = "\n".join(f"[{i}] {q}" for i, q in enumerate(questions))
    resp = client.messages.create(
        model=s.anthropic_model,
        max_tokens=min(2048, 220 * len(questions) + 120),
        system=(
            "You answer job-application questions in the candidate's own "
            "first-person voice. For EACH question return a specific, honest answer "
            "(<=120 words) grounded ONLY in the candidate's real background — never "
            "invent employers, projects, or numbers. Tie answers to THIS company "
            "and role where relevant. No preamble, no placeholders like [your name], "
            "no sign-off. Return one entry per question id."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Role: {title} at {company}.\n"
                f"Candidate: {identity_block or '(not provided)'}\n"
                f"Background:\n{background or '(not provided)'}\n"
                + (f"What I've done (cite these specifics, don't invent others):\n"
                   f"{knowledge_block}\n" if knowledge_block else "")
                + f"Job description (may be truncated):\n{desc or '(not provided)'}\n\n"
                f"QUESTIONS:\n{listing}"
            ),
        }],
        output_config={"format": {"type": "json_schema", "schema": _QA_SCHEMA}},
    )
    payload = next((b.text for b in resp.content if b.type == "text"), "")
    out = [None] * len(questions)
    for item in json.loads(payload).get("answers", []):
        i = item.get("id")
        if isinstance(i, int) and 0 <= i < len(questions):
            out[i] = str(item.get("answer", "")).strip()
    return [a or _answer_question_template(questions[i], company, title)
            for i, a in enumerate(out)]


def draft_application_answers(
    company: str, title: str, description: str | None, profile_row=None
) -> str:
    """A short "why I'm a fit" blurb the user can paste into an application."""
    background = ""
    if profile_row is not None:
        from .profile import profile_text

        background = profile_text(profile_row)
    if get_settings().use_llm_router:
        try:
            return _draft_application_via_claude(company, title, description, background)
        except Exception:
            logger.exception("Claude application draft failed; using template")
    return _draft_application_template(company, title, background)


def answer_application_question(
    question: str, company: str, title: str, description: str | None,
    profile_row=None, *, identity_block: str = "", knowledge_block: str = "",
) -> str:
    """Draft an answer to one free-text application question."""
    background = ""
    if profile_row is not None:
        from .profile import profile_text

        background = profile_text(profile_row)
    if get_settings().use_llm_router:
        try:
            return _answer_question_via_claude(
                question, company, title, description, background, identity_block,
                knowledge_block,
            )
        except Exception:
            logger.exception("Claude question answer failed; using template")
    return _answer_question_template(question, company, title)


def _answer_question_template(question: str, company: str, title: str) -> str:
    role = title or "this role"
    return (
        f"I'm genuinely excited about {role} at {company}. "
        f"(Re: \"{question.strip()}\") I'd bring strong ownership and a track "
        "record of shipping, and I'm eager to contribute and grow with the team."
    )


def _answer_question_via_claude(
    question: str, company: str, title: str, description: str | None,
    background: str, identity_block: str, knowledge_block: str = "",
) -> str:
    import anthropic

    from . import llm_budget

    if not llm_budget.consume():
        raise RuntimeError("llm user daily cap")
    s = get_settings()
    client = anthropic.Anthropic(api_key=s.anthropic_api_key)
    desc = (description or "").strip()[:1000]
    resp = client.messages.create(
        model=s.anthropic_model,
        max_tokens=320,
        system=(
            "You answer a single job-application question in the candidate's own "
            "first-person voice. Be specific, honest, and concise (under 700 "
            "characters unless the question clearly needs more). Ground every claim "
            "in the candidate's background — never invent experience, employers, or "
            "numbers. No preamble, no placeholders like [your name], no sign-off — "
            "output only the answer text."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Question: {question.strip()}\n\n"
                f"Role: {title} at {company}.\n"
                f"Candidate: {identity_block or '(not provided)'}\n"
                f"Background:\n{background or '(not provided)'}\n"
                + (f"What I've done (cite these specifics, don't invent others):\n"
                   f"{knowledge_block}\n" if knowledge_block else "")
                + f"Job description (may be truncated):\n{desc or '(not provided)'}"
            ),
        }],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "").strip()
    return text or _answer_question_template(question, company, title)


def _draft_application_template(company: str, title: str, background: str) -> str:
    role = title or "this role"
    fit = ""
    if background:
        first = background.splitlines()[0].split(":", 1)[-1].strip()
        if first:
            fit = f" My background in {first} maps closely to what this role needs, and"
    return (
        f"I'm excited to apply for {role} at {company}.{fit} I'd bring strong "
        "ownership and a track record of shipping. I'd love the chance to "
        "contribute and grow with the team."
    )


def _draft_application_via_claude(
    company: str, title: str, description: str | None, background: str
) -> str:
    import anthropic

    from . import llm_budget

    if not llm_budget.consume():
        raise RuntimeError("llm user daily cap")
    s = get_settings()
    client = anthropic.Anthropic(api_key=s.anthropic_api_key)
    desc = (description or "").strip()[:1200]
    resp = client.messages.create(
        model=s.anthropic_model,
        max_tokens=260,
        system=(
            "Write a single short 'why I'm a great fit' paragraph for a job "
            "application, in the candidate's own first-person voice. Under 600 "
            "characters, specific to the role, no subject line, no placeholders "
            "like [your name], no preamble — output only the paragraph."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Role: {title} at {company}.\n"
                f"My background:\n{background or '(not provided)'}\n"
                f"Job description (may be truncated):\n{desc or '(not provided)'}"
            ),
        }],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "").strip()
    return text or _draft_application_template(company, title, background)
