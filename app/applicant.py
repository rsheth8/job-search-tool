"""Applicant identity for application autofill (Track C).

The job-search profile says what roles you want; this says who you *are* on an
application form — the plain facts every ATS asks for (name, email, phone,
location, links, work authorization). The browser-extension autofill maps these
onto a live form's fields when you focus them.

Stored as a JSON blob in ``job_search_profile.applicant_json`` so adding a field
never needs a migration. Deliberately **excludes EEO / demographic questions**
(race, gender, disability, veteran status) — those are sensitive and stay a manual
choice; we never pre-fill them.
"""
from __future__ import annotations

import json

from . import profile as profile_mod

# Known identity keys the autofill understands. Free-form, all optional — an empty
# value just means "no suggestion for that field".
#   bools (work_authorized / needs_sponsorship) are stored as real booleans.
TEXT_FIELDS = (
    "first_name", "last_name", "full_name", "email", "phone",
    "city", "state", "country", "linkedin", "github", "portfolio",
    "school", "grad_year", "years_experience",
)
BOOL_FIELDS = ("work_authorized", "needs_sponsorship")
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
    Bools become 'Yes'/'No' strings (what most ATS dropdowns expect)."""
    out: dict[str, object] = {}
    for k, v in get_identity(user_id).items():
        if k in BOOL_FIELDS:
            out[k] = "Yes" if v else "No"
        elif v not in (None, ""):
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
    return str(v).strip().lower() in ("1", "true", "yes", "y", "authorized")
