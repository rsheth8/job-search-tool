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
from datetime import datetime, timezone

from . import profile as profile_mod

# Known identity keys the autofill understands. Free-form, all optional — an empty
# value just means "no suggestion for that field".
#   bools are stored as real booleans.
TEXT_FIELDS = (
    # name + contact. ``middle_name`` is asked for on a surprising number of
    # forms and matched nothing before, so that box was always left blank.
    "first_name", "middle_name", "last_name", "full_name", "preferred_name",
    "pronouns", "email", "phone", "phone_type",
    # location. ``address2`` is the apartment/suite line: "Address line 2" was
    # matching the ``address`` rule, so street address got painted into it.
    "address", "address2", "city", "state", "zip", "country", "location",
    # links
    "linkedin", "github", "portfolio",
    # education. ``grad_month`` is asked for separately because Greenhouse and
    # Workable split the education end date into a month select and a year
    # select; deriving the month from a bare "2027" is impossible, so that half
    # of every education block used to go unanswered.
    "school", "degree", "discipline", "gpa", "grad_year", "grad_month",
    "intern_season",
    # experience
    "current_company", "current_title", "years_experience",
    # logistics commonly asked on applications
    "salary_expectation", "start_date", "work_arrangement", "how_heard",
    "employment_type", "referral_name",
    # Work authorization has two halves. The bools below answer "are you
    # authorized" and "do you need sponsorship"; this answers the dropdown
    # asking *which* status, which student forms ask constantly (F-1 OPT,
    # STEM OPT, CPT, H-1B, TN). Citizenship is deliberately not a field here:
    # it stays in the never-fill list next to national origin.
    "work_auth_type", "security_clearance",
    # Free lists, same comma-separated shape as skills.
    "languages", "certifications",
    # optional EEO (only filled when set — see fieldmatch)
    "gender", "race",     "ethnicity", "hispanic_latino", "veteran_status", "disability_status",
)
# Yes/No questions. Rendered as "Yes"/"No" for selects/radios by autofill_map.
BOOL_FIELDS = (
    "work_authorized", "needs_sponsorship", "willing_to_relocate",
    "background_check", "drug_test", "over_18", "can_travel",
    "previously_applied", "related_to_employee", "hispanic_latino",
    "drivers_license",
)
FIELDS = TEXT_FIELDS + BOOL_FIELDS

# --- education -------------------------------------------------------------
# Applications ask about education one block at a time, and people routinely
# have two degrees in flight: a bachelor's finishing while a master's is under
# way, or one earned and the next in progress. A single flat set of fields
# cannot say that, so ``education`` holds a list and the flat keys above are
# *derived* from it. Nothing migrates -- applicant_json is schemaless -- and a
# profile that has never touched the list keeps behaving exactly as before.
EDUCATION_FIELDS = ("school", "degree", "discipline", "gpa",
                    "start_year", "grad_month", "grad_year", "status")
EDUCATION_STATUSES = ("in_progress", "completed")
#: Flat keys computed from the list whenever there is one. Writing these
#: directly is still supported; ``set_identity`` routes them into the entry
#: they describe rather than storing a value the next read would overwrite.
DERIVED_EDUCATION = ("school", "degree", "discipline", "gpa",
                     "grad_year", "grad_month")
MAX_EDUCATION = 6

_IN_PROGRESS_WORDS = frozenset({
    "in_progress", "inprogress", "current", "ongoing", "pursuing", "expected",
    "present", "attending",
})
_COMPLETED_WORDS = frozenset({
    "completed", "complete", "done", "graduated", "earned", "awarded", "past",
})


def _year_of(value) -> int | None:
    m = re.search(r"((?:19|20)\d{2})", str(value or ""))
    return int(m.group(1)) if m else None


def _this_year() -> int:
    return datetime.now(timezone.utc).year


def _clean_entry(raw) -> dict:
    """One education entry, keys trimmed to the known set."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key in EDUCATION_FIELDS:
        value = raw.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            out[key] = text
    if not out:
        return {}
    word = out.get("status", "").lower().replace("-", "_").replace(" ", "_")
    if word in _IN_PROGRESS_WORDS:
        out["status"] = "in_progress"
    elif word in _COMPLETED_WORDS:
        out["status"] = "completed"
    else:
        # An unrecognised word is worse than none: it would be believed.
        out.pop("status", None)
    return out


def clean_education(raw) -> list[dict]:
    """Validate a list of entries, dropping blanks and exact duplicates."""
    if not isinstance(raw, (list, tuple)):
        return []
    seen: set[tuple] = set()
    out: list[dict] = []
    for item in raw:
        entry = _clean_entry(item)
        if not entry:
            continue
        key = tuple(entry.get(k, "").lower()
                    for k in ("school", "degree", "discipline"))
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
        if len(out) >= MAX_EDUCATION:
            break
    return out


def is_in_progress(entry: dict) -> bool:
    """Whether this degree is still being read for.

    An explicit status wins. Otherwise a graduation year later than this one
    means in progress, and a start year with no end at all means the same. A
    degree ending *this* year is genuinely ambiguous without a month, and is
    called finished -- the conservative answer, since claiming to still be
    enrolled is the more embarrassing of the two on an application.
    """
    status = entry.get("status")
    if status in EDUCATION_STATUSES:
        return status == "in_progress"
    year = _year_of(entry.get("grad_year"))
    if year is None:
        return bool(entry.get("start_year"))
    return year > _this_year()


def order_education(entries: list[dict]) -> list[dict]:
    """Most relevant first: in progress, then most recently finished."""
    return sorted(
        entries,
        key=lambda e: (0 if is_in_progress(e) else 1,
                       -(_year_of(e.get("grad_year")) or 0)),
    )


def education_summary(entries: list[dict]) -> str:
    """One line for the many forms with a single free-text education box."""
    parts = []
    for entry in order_education(clean_education(entries)):
        head = " ".join(p for p in (entry.get("degree"), entry.get("discipline")) if p)
        school = entry.get("school")
        segment = f"{head}, {school}" if head and school else (head or school or "")
        if not segment:
            continue
        year = entry.get("grad_year")
        if is_in_progress(entry):
            segment += f" (expected {year})" if year else " (in progress)"
        elif year:
            segment += f" ({year})"
        parts.append(segment)
    return "; ".join(parts)


def _derived_education(entries: list[dict]) -> dict:
    """Flat fields for the entry a form is most likely asking about."""
    ordered = order_education(entries)
    if not ordered:
        return {}
    primary = ordered[0]
    out = {k: primary[k] for k in DERIVED_EDUCATION if primary.get(k)}
    # School, degree, discipline and dates are deliberately *not* backfilled
    # from other entries: a form's education block describes one degree, and
    # mixing a master's title with a bachelor's school is how wrong facts get
    # onto real applications. GPA is the exception -- it is usually asked as a
    # standalone question, and a degree still in progress rarely has one yet,
    # so a finished degree's GPA stands in. Only a finished one: an interim
    # average is not what the question means.
    if not out.get("gpa"):
        earned = next((e for e in ordered
                       if e.get("gpa") and not is_in_progress(e)), None)
        if earned:
            out["gpa"] = earned["gpa"]
    summary = education_summary(ordered)
    if summary:
        out["degrees"] = summary
    return out


def _entry_from_flat(data: dict) -> dict:
    """The single degree an older profile stored, as a list entry, so every
    caller sees one shape whether or not this profile has used the list yet."""
    return _clean_entry({k: data.get(k) for k in EDUCATION_FIELDS})


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
    entries = clean_education(data.get("education"))
    if entries:
        data["education"] = order_education(entries)
        data.update(_derived_education(entries))
    else:
        # Never stored, so derive nothing and change nothing -- just show the
        # one degree this profile does have in the shape callers now expect.
        single = _entry_from_flat(data)
        data["education"] = [single] if single else []
    return data


def set_identity(user_id: str, fields: dict) -> dict:
    """Merge ``fields`` into the saved identity (partial update). Unknown keys are
    dropped; bool fields are coerced; empty strings clear a field."""
    current = _decode(_raw(user_id))
    if "education" in fields:
        entries = clean_education(fields["education"])
        if entries:
            current["education"] = entries
        else:
            current.pop("education", None)
        for key in DERIVED_EDUCATION:
            current.pop(key, None)
    stored = clean_education(current.get("education"))
    if stored and "education" not in fields:
        # A client that predates the list still writes flat education fields.
        # Route them into the entry they describe instead of storing a value
        # the next read would derive straight over the top of.
        #
        # Only when the list is not in the same call. A resume import sends
        # both, and its flat `degree` is the summary of every degree found
        # ("M.S., B.S.") -- routing that into the first entry would overwrite
        # the specific degree with a list of all of them.
        flat = {k: v for k, v in fields.items() if k in DERIVED_EDUCATION}
        if flat:
            ordered = order_education(stored)
            for key, value in flat.items():
                text = "" if value is None else str(value).strip()
                if text:
                    ordered[0][key] = text
                else:
                    ordered[0].pop(key, None)
            current["education"] = ordered
    for k, v in fields.items():
        if k not in FIELDS:
            continue
        if stored and k in DERIVED_EDUCATION:
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
        if k == "education":
            # Structure, not a form value. Its flat derivations are already in
            # here; painting a list of dicts onto an input would be nonsense.
            continue
        if k in BOOL_FIELDS:
            out[k] = "Yes" if v else "No"
        elif v not in (None, ""):
            if k == "phone":
                digits = re.sub(r"\D+", "", str(v))
                out[k] = digits or v
            else:
                out[k] = v
    # A month/year the person picked explicitly wins; otherwise split whatever
    # they typed into grad_year ("December 2027" → December + 2027).
    month, year = _split_grad(str(out.get("grad_year") or ""))
    if month and not out.get("grad_month"):
        out["grad_month"] = month
    if year:
        out["grad_year_num"] = year
    return out


_MONTH_CANON = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _split_grad(raw: str) -> tuple[str | None, str | None]:
    """'December 2027' → ('December', '2027') for month/year dropdowns."""
    if not raw:
        return None, None
    year_m = re.search(r"((?:19|20)\d{2})", raw)
    year = year_m.group(1) if year_m else None
    low = raw.lower()
    month = None
    for name in _MONTH_CANON:
        if name.lower() in low or (len(name) >= 3 and name[:3].lower() in low.split()):
            month = name
            break
    return month, year


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
