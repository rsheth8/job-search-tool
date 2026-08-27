"""First-run setup status for the iOS wizard."""
from __future__ import annotations

from . import applicant, knowledge, profile

# Enough identity that autofill can fill a typical Greenhouse form.
_IDENTITY_READY = 0.5


def status(user_id: str) -> dict:
    has = profile.has_profile(user_id)
    audit = knowledge.audit(user_id)
    counts = audit["knowledge_counts"]
    identity_ok = float(audit["score"] or 0) >= _IDENTITY_READY
    complete = bool(has and identity_ok)
    return {
        "complete": complete,
        # Wizard only until they can receive matches. Identity gaps live in About me.
        "needs_setup": not has,
        "has_profile": has,
        "identity_score": audit["score"],
        "identity_missing": audit["identity_missing"],
        "identity_have": audit["identity_have"],
        "knowledge_counts": counts,
        "profile": profile.public_fields(user_id),
        "identity": {
            k: _stringify(v)
            for k, v in applicant.get_identity(user_id).items()
            if k in (
                "first_name", "last_name", "email", "phone", "city", "state",
                "school", "degree", "grad_year", "linkedin", "years_experience",
                "work_authorized", "needs_sponsorship",
            ) and v not in (None, "")
        },
    }


def _stringify(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
