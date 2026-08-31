"""Horizon's grounded answer for turns the command grammar can't handle.

Every other Claude call site in this app is a *transform*: score this posting,
extract that resume, draft this letter. None of them answer a question. So any
turn outside the heuristic router's grammar hit a dead end --

    "I didn't fully understand that. Try "show new jobs"..."

-- even for questions the app has every fact needed to answer ("which of these
should I do first?", "why did you show me the Databricks one?").

This module fills that hole and nothing else. The heuristic router still owns
every command it can parse: paying a model to recognise "applied to stripe"
would be waste, and it would make a reliable path unreliable. Horizon only sees
turns that were *already* lost, and it sees them with the person's real profile,
matches and applications attached, so it answers from their data instead of
guessing. That grounding is the whole point -- asked cold, the model invents
features this app doesn't have.

With no ``ANTHROPIC_API_KEY``, or once the daily chat slice is spent,
``answer`` returns ``None`` and the caller keeps the reply it already had, so
behaviour is byte-identical to before.
"""
from __future__ import annotations

import logging

from .config import get_settings

logger = logging.getLogger("horizon")

#: Answers are conversational, not documents. Haiku will happily write six
#: paragraphs about a job search; this app shows replies in a chat bubble.
MAX_TOKENS = 400

#: How much of the person's world to put in the prompt. Enough to reason over,
#: small enough to stay cheap on every turn.
TOP_MATCHES = 8
RECENT_APPS = 6

SYSTEM = """You are Horizon, the assistant inside JobPilot, an iOS app that \
finds job matches and autofills real application forms.

Answer using ONLY the facts in the CONTEXT block. It is the person's real data.
If the answer isn't there, say what you'd need instead of guessing.

Hard rules:
- Never invent a company, job, score, date or number that isn't in CONTEXT.
- JobPilot never submits an application. It fills the form; the person taps
  Submit. Never say or imply that it applies for them.
- The person attaches their resume file themselves -- iOS won't let an app do it.
- Autofill deliberately skips demographic / EEO questions.
- There is no cover-letter step in onboarding.
- Don't recommend other job boards or tools. Work with what's here.

Style: two to four sentences, warm and concrete, no lists, no headings, no
emoji. Name real companies and titles from CONTEXT when they're relevant. If
they should tap something, name the tab: Apply, You, Ask, or Settings."""


def is_available() -> bool:
    """True when a paid call could succeed (key present *and* model plausible).

    Chat behaviour is unchanged when this is false. ``use_llm_router`` also
    rejects an implausible ANTHROPIC_MODEL, so a typo shows up as "Horizon off"
    in /health instead of one failed call per turn.
    """
    return get_settings().use_llm_router


def answer(user_id: str, question: str) -> str | None:
    """Grounded reply to a free-form question, or None to keep the fallback."""
    q = (question or "").strip()
    if not q or not is_available():
        return None

    s = get_settings()
    from . import llm_budget

    # Charged to the chat slice, so a chatty session can't eat the budget that
    # discovery scoring needs (see app.llm_budget).
    if not llm_budget.consume(user_id, feature="chat"):
        logger.info("horizon: chat budget spent for %s", user_id)
        return None

    try:
        from . import llm_health

        client = llm_health.client(s.anthropic_api_key)
        resp = client.messages.create(
            model=s.anthropic_model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            messages=[{
                "role": "user",
                "content": f"CONTEXT\n{context_block(user_id)}\n\nQUESTION\n{q[:600]}",
            }],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        text = text.strip()
        return text or None
    except Exception:  # noqa: BLE001 -- a dead API must not eat the turn
        logger.info("horizon answer failed; keeping heuristic reply", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------

def context_block(user_id: str) -> str:
    """The person's real state, as plain lines. Built defensively: a failure in
    any one section drops that section rather than the whole answer."""
    parts: list[str] = []
    for build in (_profile_lines, _match_lines, _application_lines, _setup_lines):
        try:
            parts += build(user_id)
        except Exception:  # noqa: BLE001
            logger.info("horizon context section failed", exc_info=True)
    return "\n".join(parts) if parts else "(no profile data yet)"


def _profile_lines(user_id: str) -> list[str]:
    from . import profile as profile_mod

    row = profile_mod.get_profile(user_id)
    if row is None:
        return ["Search profile: not set up yet."]
    out = ["Their search profile:"]
    for label, key in (("target roles", "roles"), ("skills", "keywords"),
                       ("locations", "locations"), ("seniority", "seniority")):
        val = (_get(row, key) or "").strip()
        if val:
            out.append(f"- {label}: {val}")
    bar = _get(row, "min_relevance")
    out.append(f"- alert threshold: {bar if bar is not None else get_settings().job_relevance_threshold}"
               " (a match must score this or higher to be surfaced)")
    return out


def _match_lines(user_id: str) -> list[str]:
    from . import jobstore

    posts = jobstore.list_postings(
        user_id, statuses=("queued", "alerted", "new"),
        limit=TOP_MATCHES, exclude_already_applied=True,
    )
    counts = jobstore.counts_by_status(user_id)
    total = counts.get("queued", 0) + counts.get("alerted", 0)
    if not posts:
        return ["", f"Matches waiting: none yet (queue total {total})."]
    out = ["", f"Their top matches right now ({total} waiting on the Apply tab):"]
    for p in posts:
        score = _get(p, "relevance_score")
        pct = f"{round(float(score) * 100)}%" if score is not None else "unscored"
        loc = (_get(p, "location") or "").strip()
        out.append(f"- {_get(p, 'title')} at {_get(p, 'company')}"
                   f"{' (' + loc + ')' if loc else ''} — match {pct},"
                   f" status {_get(p, 'status')}")
    return out


def _application_lines(user_id: str) -> list[str]:
    from . import store

    apps = store.list_applications(user_id, limit=RECENT_APPS)
    if not apps:
        return ["", "Applications logged: none yet."]
    out = ["", f"Applications they've logged ({len(apps)} most recent):"]
    for a in apps:
        role = (_get(a, "role") or "").strip()
        out.append(f"- {_get(a, 'company')}{' — ' + role if role else ''}:"
                   f" {_get(a, 'status')}, updated {(_get(a, 'last_updated_at') or '')[:10]}")
    return out


def _setup_lines(user_id: str) -> list[str]:
    from . import onboarding

    st = onboarding.status(user_id)
    missing = st.get("identity_missing") or []
    score = st.get("identity_score")
    out = [""]
    if score is not None:
        out.append(f"Autofill profile completeness: {round(float(score) * 100)}%.")
    if missing:
        out.append("Still empty (Autofill will skip these): "
                   + ", ".join(str(m) for m in missing[:10]) + ".")
    else:
        out.append("Their Autofill profile is complete.")
    return out


def _get(row, key, default=None):
    """sqlite3.Row has no .get, and these come from several call paths."""
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default
