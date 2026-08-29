"""First-run profile quiz status for the iOS app.

A brand-new sign-in is held in the quiz until they finish (or skip through).
Existing users who already have a search profile are left alone. Saving roles
mid-quiz does not dismiss the quiz — only an explicit complete does.
"""
from __future__ import annotations

from . import applicant, knowledge, profile

# Prefs key. ``started`` keeps a new member in the quiz after the first save;
# ``complete`` lets them into the rest of the app.
_ONBOARDING = "onboarding"

# Enough identity that autofill can fill a typical Greenhouse form.
_IDENTITY_READY = 0.5


def status(user_id: str) -> dict:
    has = profile.has_profile(user_id)
    state = (profile.get_prefs(user_id).get(_ONBOARDING) or "").strip().lower()
    audit = knowledge.audit(user_id)
    counts = audit["knowledge_counts"]
    identity_ok = float(audit["score"] or 0) >= _IDENTITY_READY
    complete = bool(has and identity_ok)
    return {
        "complete": complete,
        "needs_setup": _needs_setup(has, state),
        "onboarding": state or None,
        "has_profile": has,
        "identity_score": audit["score"],
        "identity_missing": audit["identity_missing"],
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
