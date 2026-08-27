"""Recruiter persistence + outreach drafting (Phase 3).

Two responsibilities, both offline-safe:

  * **Persistence** — store/list recruiters discovered for a company (deduped),
    and a cached ``discover_for_company`` that only hits Apollo once per company.
  * **Drafting** — turn a recruiter + application into a short outreach message.
    Uses Claude when an API key is present (better phrasing); otherwise a clean
    template. The template is what the test suite exercises, so it must stand on
    its own.

We deliberately **do not send anything**. There's no LinkedIn/email send channel
wired up, so the drafted message is the product: the user copy/pastes it. The
spec's "never send without explicit confirmation" is satisfied trivially today
(nothing is ever sent); a real send path would add the confirm step here.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from . import apollo, store
from .config import get_settings
from .db import connect

logger = logging.getLogger("outreach")


@dataclass
class DiscoveryResult:
    """Outcome of ``discover_for_company`` — recruiters plus Apollo cost metadata."""

    recruiters: list[sqlite3.Row]
    from_cache: bool = False
    people_search: bool = False
    org_credits: int = 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def list_recruiters(user_id: str, company: str) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM recruiters WHERE user_id = ? AND lower(company) = lower(?) "
            "ORDER BY id",
            (user_id, company),
        ).fetchall()


def has_recruiters(user_id: str, company: str) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM recruiters WHERE user_id = ? AND lower(company) = lower(?) "
            "LIMIT 1",
            (user_id, company),
        ).fetchone()
        return row is not None


def store_recruiters(
    user_id: str,
    company: str,
    people: list[dict],
    *,
    application_id: int | None = None,
    source: str = "apollo",
) -> list[sqlite3.Row]:
    """Insert recruiters for a company, skipping ones we already have.

    Dedupe on ``apollo_person_id`` when present, else ``name``. Updates sparse
    rows when Apollo returns richer data on a later call.
    """
    with connect() as conn:
        for p in people:
            name = (p.get("name") or "").strip()
            if not name:
                continue
            apollo_id = (p.get("apollo_person_id") or "").strip() or None
            existing = _find_existing_recruiter(
                conn, user_id, company, name, apollo_id
            )
            if existing:
                _maybe_enrich_recruiter(conn, existing["id"], p)
                continue
            conn.execute(
                """
                INSERT INTO recruiters
                    (user_id, application_id, company, name, title, email,
                     linkedin_url, source, apollo_person_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    application_id,
                    p.get("company") or company,
                    name,
                    p.get("title"),
                    p.get("email"),
                    p.get("linkedin_url"),
                    source,
                    apollo_id,
                    _now_iso(),
                ),
            )
    return list_recruiters(user_id, company)


def _find_existing_recruiter(
    conn: sqlite3.Connection,
    user_id: str,
    company: str,
    name: str,
    apollo_id: str | None,
) -> sqlite3.Row | None:
    if apollo_id:
        row = conn.execute(
            "SELECT id FROM recruiters WHERE user_id = ? AND lower(company) = lower(?) "
            "AND apollo_person_id = ? LIMIT 1",
            (user_id, company, apollo_id),
        ).fetchone()
        if row:
            return row
    return conn.execute(
        "SELECT id FROM recruiters WHERE user_id = ? AND lower(company) = lower(?) "
        "AND lower(name) = lower(?) LIMIT 1",
        (user_id, company, name),
    ).fetchone()


def _maybe_enrich_recruiter(
    conn: sqlite3.Connection, recruiter_id: int, p: dict
) -> None:
    """Fill in fields we didn't have on a prior partial Apollo response."""
    row = conn.execute(
        "SELECT title, email, linkedin_url, apollo_person_id FROM recruiters WHERE id = ?",
        (recruiter_id,),
    ).fetchone()
    if not row:
        return
    updates: dict[str, object] = {}
    for field in ("title", "email", "linkedin_url"):
        new_val = p.get(field)
        if new_val and not row[field]:
            updates[field] = new_val
    if p.get("apollo_person_id") and not row["apollo_person_id"]:
        updates["apollo_person_id"] = p["apollo_person_id"]
    if not updates:
        return
    sets = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE recruiters SET {sets} WHERE id = ?",
        (*updates.values(), recruiter_id),
    )


def discover_for_company(
    user_id: str,
    company: str,
    *,
    application_id: int | None = None,
    limit: int = 3,
) -> DiscoveryResult:
    """Return recruiters for a company, discovering via Apollo if we have none.

    Cached: if we've already stored recruiters for this company we return them
    without re-hitting Apollo. Empty list when Apollo is unconfigured or finds
    nobody — the caller handles that gracefully.
    """
    existing = list_recruiters(user_id, company)
    if existing:
        return DiscoveryResult(recruiters=existing, from_cache=True)

    cap = get_settings().apollo_max_results
    effective = min(limit, cap)
    people = apollo.discover_recruiters(company, limit=effective)
    meta = apollo.discovery_meta()
    if not people:
        return DiscoveryResult(
            recruiters=[],
            people_search=meta.people_search,
            org_credits=meta.org_credits,
        )
    rows = store_recruiters(
        user_id, company, people, application_id=application_id
    )
    return DiscoveryResult(
        recruiters=rows,
        people_search=meta.people_search or bool(people),
        org_credits=meta.org_credits,
    )


def apollo_footnote(result: DiscoveryResult) -> str:
    """One-line SMS note about Apollo cost / cache for transparency."""
    if result.from_cache:
        return "Apollo: saved contacts — no API call."
    if result.org_credits:
        n = result.org_credits
        credit_word = "credit" if n == 1 else "credits"
        return (
            f"Apollo: people search (no credits) + {n} org lookup {credit_word} "
            "(domain cached — won't repeat)."
        )
    if result.people_search:
        return "Apollo: people search — no credits used."
    return ""


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------

def draft_outreach(
    company: str, recruiter: sqlite3.Row | dict, *, role: str | None = None
) -> str:
    """A short outreach message to ``recruiter`` about ``company``.

    Claude when a key is configured, otherwise a template. Any LLM failure falls
    back to the template — drafting never hard-fails.
    """
    name = _value(recruiter, "name") or "there"
    if get_settings().use_llm_router:
        try:
            return _draft_via_claude(company, name, role)
        except Exception:  # network/auth/parse — fall back, never block
            logger.exception("Claude draft failed; using template")
    return _draft_template(company, name, role)


def _value(rec: sqlite3.Row | dict, key: str):
    if isinstance(rec, dict):
        return rec.get(key)
    try:
        return rec[key]
    except (IndexError, KeyError):
        return None


def _first_name(name: str) -> str:
    parts = (name or "").split()
    return parts[0] if parts else "there"


def _draft_template(company: str, name: str, role: str | None) -> str:
    first = _first_name(name)
    role_phrase = f" for the {role} role" if role else ""
    return (
        f"Hi {first}, I recently applied{role_phrase} at {company} and I'm really "
        f"excited about the team. I'd love to connect and learn more about the "
        f"process. Would you be open to a quick chat? Thanks!"
    )


def _draft_via_claude(company: str, name: str, role: str | None) -> str:
    import anthropic

    from . import llm_budget

    if not llm_budget.consume():
        raise RuntimeError("llm user daily cap")
    s = get_settings()
    client = anthropic.Anthropic(api_key=s.anthropic_api_key)
    first = _first_name(name)
    role_phrase = f" for the {role} role" if role else ""
    resp = client.messages.create(
        model=s.anthropic_model,
        max_tokens=200,
        system=(
            "Write a single short, warm, professional LinkedIn connection note "
            "from a job candidate to a recruiter. Under 300 characters, no "
            "subject line, no placeholders, no preamble — output only the message."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Recruiter first name: {first}. Company: {company}."
                f"{(' Role I applied for:' + role_phrase) if role else ''} "
                "I already applied and want to connect and learn about the process."
            ),
        }],
    )
    text = next((b.text for b in resp.content if b.type == "text"), "").strip()
    return text or _draft_template(company, name, role)


# ---------------------------------------------------------------------------
# Assisted apply (Phase 2): draft a "why I'm a fit" blurb for a posting
# ---------------------------------------------------------------------------

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
    """Draft a tailored answer for EACH question in one batched Haiku call (cheap;
    one round-trip for the whole application). Grounded in the candidate's
    background + identity + the JD. Falls back to per-question templates on any
    failure — never hard-fails, and always returns one answer per question."""
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
    # Fill any the model skipped with a template so the list is always complete.
    return [a or _answer_question_template(questions[i], company, title)
            for i, a in enumerate(out)]


def draft_application_answers(
    company: str, title: str, description: str | None, profile_row=None
) -> str:
    """A short "why I'm a fit" blurb the user can paste into an application.

    Claude when a key is configured (tailored to the posting + the user's
    background); otherwise a clean template. Mirrors ``draft_outreach``: any LLM
    failure falls back to the template — drafting never hard-fails.
    """
    background = ""
    if profile_row is not None:
        from .profile import profile_text

        background = profile_text(profile_row)
    if get_settings().use_llm_router:
        try:
            return _draft_application_via_claude(company, title, description, background)
        except Exception:  # network/auth/parse — fall back, never block
            logger.exception("Claude application draft failed; using template")
    return _draft_application_template(company, title, background)


def answer_application_question(
    question: str, company: str, title: str, description: str | None,
    profile_row=None, *, identity_block: str = "", knowledge_block: str = "",
) -> str:
    """Draft an answer to ONE free-text application question ("Why do you want to
    work here?", "Describe a hard problem you solved"), grounded in the candidate's
    background. Claude when keyed; otherwise a generic-but-usable template. Never
    hard-fails — the browser autofill always gets *something* to show + edit."""
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
        except Exception:  # network/auth/parse — fall back, never block the form
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
        # profile_text lines look like "Roles: backend swe" — use the value.
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
