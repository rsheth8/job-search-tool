"""First-run profile quiz status for the iOS app.

A brand-new sign-in is held in the quiz until they finish (or skip through).
Existing users who already have a search profile are left alone. Saving roles
mid-quiz does not dismiss the quiz — only an explicit complete does.
"""
from __future__ import annotations

import json

from . import applicant, knowledge, profile

# Prefs key. ``started`` keeps a new member in the quiz after the first save;
# ``complete`` lets them into the rest of the app.
_ONBOARDING = "onboarding"

# Enough identity that autofill can fill a typical Greenhouse form.
_IDENTITY_READY = 0.5

# Fields Autofill genuinely cannot work without. The fraction alone isn't
# enough: school, links and location add up to 50% while leaving no name and no
# email, and every application form on earth opens with those three.
_CORE_IDENTITY = (
    ("first_name", "first name"),
    ("last_name", "last name"),
    ("email", "email"),
)


def status(user_id: str) -> dict:
    has = profile.has_profile(user_id)
    state = (profile.get_prefs(user_id).get(_ONBOARDING) or "").strip().lower()
    audit = knowledge.audit(user_id)
    counts = audit["knowledge_counts"]
    identity_ok = float(audit["score"] or 0) >= _IDENTITY_READY
    identity = applicant.get_identity(user_id)
    core_missing = [
        human for key, human in _CORE_IDENTITY
        if identity.get(key) in (None, "")
    ]
    complete = bool(has and identity_ok and not core_missing)
    return {
        "complete": complete,
        "needs_setup": _needs_setup(has, state),
        "onboarding": state or None,
        "has_profile": has,
        "identity_score": audit["score"],
        "identity_missing": audit["identity_missing"],
        "identity_core_missing": core_missing,
        "identity_have": audit["identity_have"],
        "knowledge_counts": counts,
        "profile": profile.public_fields(user_id),
        "identity": _identity_payload(user_id),
    }


def mark_started(user_id: str) -> dict:
    """Pin a new member in the quiz. No-op once they've finished."""
    if (profile.get_prefs(user_id).get(_ONBOARDING) or "") != "complete":
        profile.update_prefs(user_id, **{_ONBOARDING: "started"})
    return status(user_id)


def mark_complete(user_id: str) -> dict:
    """Let them into the app. Skipped quiz steps stay skipped."""
    profile.update_prefs(user_id, **{_ONBOARDING: "complete"})
    return status(user_id)


def _needs_setup(has_profile: bool, state: str) -> bool:
    if state == "complete":
        return False
    if state == "started":
        return True
    # No flag: existing members with a search profile skip the new quiz.
    # Brand-new sign-ins (nothing saved yet) are sent through it.
    return not has_profile


def _identity_payload(user_id: str) -> dict:
    """All saved identity fields, plus Apple name/email as unsaved prefills."""
    saved = applicant.get_identity(user_id)
    out = {
        k: _stringify(v)
        for k, v in saved.items()
        if k in applicant.FIELDS and v not in (None, "")
    }
    try:
        from . import auth
        user = auth.get_user(user_id)
    except Exception:  # noqa: BLE001 — quiz prefill must never break setup
        user = None
    if not user:
        return out
    if "email" not in out and user.get("email"):
        out["email"] = str(user["email"])
    if "first_name" not in out and user.get("display_name"):
        first, _, last = str(user["display_name"]).strip().partition(" ")
        if first:
            out["first_name"] = first
        if last:
            out["last_name"] = last
    return out


def _stringify(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def quiz_draft(user_id: str, *, polish: bool = False) -> dict:
    """Prefill the slow quiz steps from stored knowledge and identity.

    Free by default (knowledge + a short template). ``polish`` asks Claude to
    turn that into first-person answers the person can edit — fail-open to the
    free draft when the key/budget is missing.
    """
    items = knowledge.list_all(user_id)
    ident = applicant.get_identity(user_id)
    prof = profile.public_fields(user_id)

    def first(category: str) -> str:
        for item in items:
            if item["category"] == category and (item.get("text") or "").strip():
                return str(item["text"]).strip()
        return ""

    about = ""
    why = ""
    for item in items:
        if item["category"] != "answer":
            continue
        label = (item.get("label") or "").lower()
        text = (item.get("text") or "").strip()
        if not text:
            continue
        if not about and (
            "about yourself" in label or "tell us about" in label
        ):
            about = text
        if not why and (
            "why do you want" in label
            or "why this" in label
            or "why are you" in label
        ):
            why = text

    draft = {
        "project": first("project"),
        "achievement": first("achievement") or first("experience"),
        "strength": first("strength"),
        "preference": first("preference"),
        "about": about or _about_template(ident, prof),
        "why_role": why or _why_template(ident, prof),
        "roles": prof.get("roles") or "",
        "locations": prof.get("locations") or "",
        "keywords": prof.get("keywords") or "",
        "seniority": prof.get("seniority") or "",
    }
    if polish:
        polished = _llm_polish_quiz(user_id, ident, prof, items, draft)
        if polished:
            for key in (
                "project", "achievement", "strength", "preference",
                "about", "why_role",
            ):
                value = (polished.get(key) or "").strip()
                if value:
                    draft[key] = value
    return draft


def _about_template(ident: dict, prof: dict) -> str:
    name = (ident.get("first_name") or ident.get("full_name") or "").strip()
    school = (ident.get("school") or "").strip()
    degree = (ident.get("degree") or "").strip()
    disc = (ident.get("discipline") or "").strip()
    roles = (prof.get("roles") or "software engineering roles").strip()
    who = f"I'm {name}" if name else "I'm a candidate"
    edu = ", ".join(p for p in (degree, disc) if p)
    if school and edu:
        lead = f"{who}, studying {edu} at {school}."
    elif school:
        lead = f"{who} at {school}."
    else:
        lead = f"{who}."
    return f"{lead} I'm looking for {roles}."


def _why_template(ident: dict, prof: dict) -> str:
    roles = (prof.get("roles") or "this kind of work").strip()
    return (
        f"I want to keep building in {roles} — real ownership, strong teammates, "
        "and problems I can learn from quickly."
    )


_POLISH_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        k: {"type": "string"} for k in (
            "project", "achievement", "strength", "preference", "about", "why_role",
        )
    },
}


def _llm_polish_quiz(
    user_id: str, ident: dict, prof: dict, items: list[dict], draft: dict,
) -> dict | None:
    from .config import get_settings

    s = get_settings()
    if not s.use_llm_router:
        return None
    from . import llm_budget

    llm_budget.set_user(user_id)
    if not llm_budget.consume(user_id):
        return None
    facts = []
    for item in items[:12]:
        cat = item.get("category") or ""
        text = (item.get("text") or "").strip()
        if cat in ("answer",) or not text:
            continue
        facts.append(f"- {cat}: {text[:280]}")
    blob = "\n".join(facts) or "(no stored facts yet)"
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=s.anthropic_api_key)
        resp = client.messages.create(
            model=s.anthropic_model,
            max_tokens=700,
            system=(
                "Write short first-person quiz answers from the facts given. "
                "Do not invent employers, metrics, or schools. Empty string if "
                "you lack a fact for that field. about: 2-3 sentences. "
                "why_role: 2 sentences about the kind of work, not a company."
            ),
            messages=[{"role": "user", "content": (
                f"Name: {ident.get('full_name') or ident.get('first_name') or ''}\n"
                f"School: {ident.get('school') or ''} {ident.get('degree') or ''} "
                f"{ident.get('discipline') or ''}\n"
                f"Looking for: {prof.get('roles') or ''} in {prof.get('locations') or ''}\n"
                f"Facts:\n{blob}\n\n"
                f"Current draft: {json.dumps({k: draft.get(k) for k in ('about', 'why_role', 'project')})}"
            )}],
            output_config={"format": {"type": "json_schema", "schema": _POLISH_SCHEMA}},
        )
        payload = next((b.text for b in resp.content if b.type == "text"), "")
        data = json.loads(payload)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — quiz must never fail because polish missed
        return None
