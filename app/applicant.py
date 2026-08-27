"""Applicant identity for application autofill (Track C).

The job-search profile says what roles you want; this says who you *are* on an
application form — the plain facts every ATS asks for (name, email, phone,
location, links, work authorization). The browser-extension autofill maps these
onto a live form's fields when you focus them.

Stored as a JSON blob in ``job_search_profile.applicant_json`` so adding a field
never needs a migration.

Sensitive demographics (gender, race, veteran, disability) are **optional**: they
are only filled when the user has explicitly saved a value. Other EEO topics
(sexual orientation, religion, DOB, …) stay never-filled.
"""
from __future__ import annotations

import json
import re

from . import profile as profile_mod

# Known identity keys the autofill understands. Free-form, all optional — an empty
# value just means "no suggestion for that field".
#   bools are stored as real booleans.
TEXT_FIELDS = (
    # name + contact
    "first_name", "last_name", "full_name", "preferred_name", "pronouns",
    "email", "phone",
    # location
    "address", "city", "state", "zip", "country", "location",
    # links
    "linkedin", "github", "portfolio",
    # education
    "school", "degree", "discipline", "gpa", "grad_year",
    # experience
    "current_company", "current_title", "years_experience",
    # logistics commonly asked on applications
    "salary_expectation", "start_date", "work_arrangement", "how_heard",
    # optional EEO (only filled when set — see fieldmatch)
    "gender", "race", "ethnicity", "veteran_status", "disability_status",
)
# Yes/No questions. Rendered as "Yes"/"No" for selects/radios by autofill_map.
BOOL_FIELDS = (
    "work_authorized", "needs_sponsorship", "willing_to_relocate",
    "background_check", "drug_test", "over_18", "can_travel",
    "previously_applied", "related_to_employee",
)
FIELDS = TEXT_FIELDS + BOOL_FIELDS


def get_identity(user_id: str) -> dict:
    """The saved identity dict (empty if none). ``full_name`` is derived from
    first/last when not set explicitly."""
    row = profile_mod.get_profile(user_id)
    raw = row["applicant_json"] if row is not None and "applicant_json" in row.keys() else None
    data = _decode(raw)
    if not data.get("full_name"):
        joined = " ".join(p for p in (data.get("first_name"), data.get("last_name")) if p)
        if joined:
            data["full_name"] = joined
    if not data.get("location"):
        # "Chicago, IL" / "Chicago, IL, USA" — what most "current location" fields want.
        loc = ", ".join(p for p in (data.get("city"), data.get("state"),
                                    data.get("country")) if p)
        if loc:
            data["location"] = loc
    return data


def set_identity(user_id: str, fields: dict) -> dict:
    """Merge ``fields`` into the saved identity (partial update). Unknown keys are
    dropped; bool fields are coerced; empty strings clear a field."""
    current = _decode(_raw(user_id))
    for k, v in fields.items():
        if k not in FIELDS:
            continue
        if k in BOOL_FIELDS:
            current[k] = _as_bool(v)
        elif v is None or (isinstance(v, str) and not v.strip()):
            current.pop(k, None)
        else:
            current[k] = v.strip() if isinstance(v, str) else v
    profile_mod.set_profile(user_id, applicant_json=json.dumps(current))
    return get_identity(user_id)


def autofill_map(user_id: str) -> dict:
    """Identity as a flat {field: value} map the extension paints onto a form.
    Bools become 'Yes'/'No' strings (what most ATS dropdowns expect).
    Phone is digits-only (user preference for form fields)."""
    out: dict[str, object] = {}
    for k, v in get_identity(user_id).items():
        if k in BOOL_FIELDS:
            out[k] = "Yes" if v else "No"
        elif v not in (None, ""):
            if k == "phone":
                digits = re.sub(r"\D+", "", str(v))
                out[k] = digits or v
            else:
                out[k] = v
    return out


def identity_block(user_id: str) -> str:
    """A short human-readable identity summary for grounding LLM answers."""
    d = get_identity(user_id)
    parts = []
    if d.get("full_name"):
        parts.append(d["full_name"])
    loc = ", ".join(p for p in (d.get("city"), d.get("state"), d.get("country")) if p)
    if loc:
        parts.append(loc)
    if d.get("years_experience"):
        parts.append(f"{d['years_experience']} yrs experience")
    if d.get("work_authorized") is not None:
        parts.append("work-authorized" if d["work_authorized"] else "needs work authorization")
    if d.get("needs_sponsorship"):
        parts.append("requires visa sponsorship")
    return " · ".join(parts)


# ---------------------------------------------------------------------------

def _raw(user_id: str) -> str | None:
    row = profile_mod.get_profile(user_id)
    return row["applicant_json"] if row is not None and "applicant_json" in row.keys() else None


def _decode(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y"):
        return True
    if s in ("0", "false", "no", "n"):
        return False
    return bool(s)
