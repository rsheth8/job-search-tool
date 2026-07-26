"""Personal knowledge store — what makes an answer *yours*.

The applicant identity (``applicant.py``) covers the facts a form asks for: name,
email, school, work authorization. This covers the things that make a written
answer specific instead of generic — the projects you'd actually cite, what you're
proud of, what you're good at, what you want out of a job, and the answers you've
already written well once and shouldn't rewrite every time.

Two payoffs:

* **Grounding.** ``knowledge_block`` goes into the answer drafter's prompt, so a
  drafted answer cites your real work instead of hedging.
* **Free reuse.** A saved ``answer`` matching the question is returned verbatim —
  no model call, no cost, no variance. That's the deterministic-first rule the
  rest of the pipeline follows.

Like the identity store, this deliberately holds **no demographic data**.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .db import connect

# What kind of fact this is. 'answer' is special: it carries a question in
# ``label`` and can be served verbatim when that question comes up again.
CATEGORIES = ("project", "achievement", "strength", "preference", "answer")

# How each category is introduced to the model.
_HEADINGS = {
    "project": "PROJECTS I CAN CITE",
    "achievement": "ACHIEVEMENTS",
    "strength": "STRENGTHS",
    "preference": "WHAT I WANT IN A ROLE",
}

# Deliberately minimal: only true filler. The interrogative words ("why", "what",
# "describe") are exactly what make two phrasings the *same* question, so stripping
# them collapses "Why do you want to work here?" and "Describe a conflict you
# handled" onto the same handful of tokens.
_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "is", "are",
    "be", "with", "this", "that", "please", "do", "does", "did", "you", "your",
    "i", "my", "me", "it", "us", "our", "we",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add(user_id: str, category: str, text: str, *, label: str | None = None) -> dict | None:
    """Record one fact. Returns the stored row, or None for an unknown category or
    empty text. An ``answer`` without a question to hang it on is rejected — it
    could never be matched back."""
    category = (category or "").strip().lower()
    text = (text or "").strip()
    if category not in CATEGORIES or not text:
        return None
    if category == "answer" and not (label or "").strip():
        return None
    now = _now()
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO user_knowledge (user_id, category, label, text, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, category, (label or "").strip() or None, text, now, now),
        )
        row = conn.execute("SELECT * FROM user_knowledge WHERE id = ?",
                           (cur.lastrowid,)).fetchone()
    return dict(row)


def list_all(user_id: str, *, category: str | None = None) -> list[dict]:
    sql = "SELECT * FROM user_knowledge WHERE user_id = ?"
    params: list = [user_id]
    if category:
        sql += " AND category = ?"
        params.append(category)
    sql += " ORDER BY category, id"
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def remove(user_id: str, item_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_knowledge WHERE user_id = ? AND id = ?",
            (user_id, item_id))
        return cur.rowcount > 0


def knowledge_block(user_id: str, *, limit_per_category: int = 6) -> str:
    """The stored facts as a compact prompt block, or "" when there's nothing yet.

    Kept short on purpose: this rides along on every answer-drafting call, and a
    focused handful of real specifics beats an exhaustive dump.
    """
    items = list_all(user_id)
    if not items:
        return ""
    out: list[str] = []
    for category, heading in _HEADINGS.items():
        picked = [i for i in items if i["category"] == category][:limit_per_category]
        if picked:
            out.append(heading + ":")
            out.extend(f"  - {i['text']}" for i in picked)
    saved = [i for i in items if i["category"] == "answer"][:limit_per_category]
    if saved:
        out.append("ANSWERS I'VE WRITTEN BEFORE (reuse the substance, adapt the wording):")
        for i in saved:
            out.append(f"  Q: {i['label']}")
            out.append(f"  A: {i['text']}")
    return "\n".join(out)


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if w not in _STOPWORDS and len(w) > 2}


def canned_answer(user_id: str, question: str, *, threshold: float = 0.6) -> str | None:
    """A previously-saved answer for this question, or None.

    Matched on meaningful-word overlap so "Why do you want to work at Acme?" still
    finds the answer saved for "Why do you want to work here?". The threshold is
    deliberately high — a wrong reuse is worse than drafting fresh.
    """
    asked = _tokens(question)
    if not asked:
        return None
    best, best_score = None, 0.0
    for item in list_all(user_id, category="answer"):
        saved = _tokens(item["label"])
        if not saved:
            continue
        # Scored by how much of the *saved* question the asked one covers, so a
        # stored "Why do you want to work here?" still matches "…work at Acme?"
        # where only the company-specific tail differs. Two words must line up:
        # a single shared word ("work") is coincidence, not the same question.
        shared = asked & saved
        if len(shared) < 2:
            continue
        score = len(shared) / len(saved)
        if score > best_score:
            best, best_score = item, score
    return best["text"] if best is not None and best_score >= threshold else None


# --- coverage audit ---------------------------------------------------------

# Identity fields worth nagging about: these are what actually block an autofill.
_IMPORTANT_IDENTITY = (
    ("first_name", "first name"), ("last_name", "last name"), ("email", "email"),
    ("phone", "phone"), ("location", "location (city/state)"),
    ("linkedin", "LinkedIn URL"), ("school", "school"), ("degree", "degree"),
    ("grad_year", "graduation year"), ("years_experience", "years of experience"),
    ("work_authorized", "work authorization"),
    ("needs_sponsorship", "sponsorship requirement"),
)


def audit(user_id: str) -> dict:
    """What's missing, so autofill coverage can climb toward "never asks me twice".

    Returns ``{identity_missing, identity_have, knowledge_counts, suggestions,
    score}`` — ``score`` is the fraction of important identity fields present.
    """
    from . import applicant

    identity = applicant.get_identity(user_id)
    have, missing = [], []
    for key, human in _IMPORTANT_IDENTITY:
        value = identity.get(key)
        (have if value not in (None, "") else missing).append(human)

    counts = {c: 0 for c in CATEGORIES}
    for item in list_all(user_id):
        counts[item["category"]] = counts.get(item["category"], 0) + 1

    suggestions = []
    if missing:
        suggestions.append(
            f"Add {len(missing)} missing detail{'s' if len(missing) != 1 else ''}: "
            + ", ".join(missing[:6]))
    if not counts.get("project"):
        suggestions.append(
            "Tell me a project worth citing — 'remember project: I built …'")
    if not counts.get("achievement"):
        suggestions.append(
            "Add an achievement — 'remember achievement: I cut latency 40%'")
    if not counts.get("answer"):
        suggestions.append(
            "Save an answer you've written well once so it's reused free next time")

    return {
        "identity_have": have,
        "identity_missing": missing,
        "knowledge_counts": counts,
        "suggestions": suggestions,
        "score": round(len(have) / len(_IMPORTANT_IDENTITY), 2),
    }
