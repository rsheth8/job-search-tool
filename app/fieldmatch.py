"""Shared application-form field matching (Phase 2 foundation).

The logic for "what does this form field mean, and what value goes in it" — the
same brain the browser extension's content.js uses, ported to Python so the
headless submit worker (Playwright's Python API) makes identical decisions. One
source of truth for both autofill paths.

Pure + dependency-free: a field's label text in, an identity key out; a select's
option texts + a desired value in, the best option out. Fully testable; no DOM, no
network.
"""
from __future__ import annotations

import hashlib
import json
import re

# (identity key, label regex) — kept in lockstep with extension/content.js RULES.
# Order matters: more specific patterns first (email before address, preferred
# before first, location before city).
FIELD_RULES: list[tuple[str, str]] = [
    ("email", r"e-?mail"),
    ("preferred_name", r"preferred (first )?name|nick.?name|known as|goes by"),
    # "Name (First)" is as common on ATS forms as "First name", so match both orders.
    ("first_name", r"first.?name|given.?name|legal first|name\s*\(?\s*first"),
    ("last_name", r"last.?name|family.?name|surname|name\s*\(?\s*last"),
    # `your name` is anchored: unanchored it also claims "How do you pronounce your
    # name?", which is a free-text question, not the name field. Seen on a live Ashby
    # form, where it was answered "Ada Testrun".
    ("full_name", r"full.?name|^\s*name\s*$|^\s*your name\b|legal name"),
    ("pronouns", r"pronouns"),
    # `tel` MUST stay word-bounded: unbounded, it matches "tell", so every
    # "Tell us about yourself" essay got a phone number typed into it. Caught on a
    # live Greenhouse form, where the background question was filled with 555-0100.
    ("phone", r"\b(phone|mobile|tel(ephone)?|contact number)\b"),
    ("linkedin", r"linked.?in"),
    ("github", r"git.?hub"),
    ("portfolio", r"portfolio|personal (web)?site|^\s*website\s*$|^url$|other url|"
                  r"personal url|home ?page|personal page"),
    ("location", r"\blocation\b|where are you (based|located)|city.{0,5}state|"
                 r"where do you (live|reside)|currently (based|located|reside)|based in"),
    ("address", r"street address|address line|mailing address|home address|^\s*address\b"),
    ("city", r"\bcity\b|town"),
    ("state", r"\bstate\b|province|region"),
    ("zip", r"\bzip\b|postal code|post.?code"),
    ("work_authorized", r"authori[sz]ed to work|work authori[sz]ation|legally.{0,12}work|eligible to work|right to work"),
    ("needs_sponsorship", r"sponsorship|require.{0,12}visa|visa.{0,12}status|immigration status"),
    ("country", r"\bcountry\b|nation"),
    ("school", r"school|university|college|institution|alma mater|where did you study"),
    ("degree", r"degree|qualification|level of (education|study)"),
    # "major" only where it names a course of study — a bare match also claims
    # "a major project", "a major contributor", "the major milestone".
    ("discipline", r"^\s*major\b|your major|academic major|major\s*/|"
                   r"discipline|field of study|concentration"),
    ("gpa", r"\bgpa\b|grade point"),
    ("grad_year", r"grad(uation)?.{0,8}(year|date)|class of|completion (year|date)|"
                  r"year of grad"),
    ("current_company", r"current (employer|company)|present (employer|company)|where do you (currently )?work"),
    ("current_title", r"current (title|role|position)|present (title|role|position)"),
    ("years_experience", r"years.{0,10}experience|experience.{0,10}years|\byoe\b|"
                         r"how many years"),
    ("salary_expectation", r"salary (expectation|requirement)|expected (salary|compensation|pay)|desired (salary|pay|compensation)|compensation expectation|pay expectation"),
    ("start_date", r"start date|available to start|earliest (start|availability)|"
                   r"when (can|could) you start|date available|notice period|"
                   r"availability date"),
    # These two ask about the *candidate's* status. The bare stems (`relocat`,
    # `sponsor`) also matched any essay that happened to mention relocating a
    # service or sponsoring a project, so both are scoped to the asking phrasing.
    ("willing_to_relocate", r"willing to relocate|open to relocat|able to relocate|"
                            r"would you relocate|relocation (required|assistance)|"
                            r"\brelocate\?"),
]
_COMPILED = [(key, re.compile(pat, re.I)) for key, pat in FIELD_RULES]

# Demographic / EEO fields we NEVER auto-fill (sensitive — left to the human).
# Broad on purpose: a false positive costs one manually-filled field, a false
# negative answers a protected-class question on the user's behalf.
_NEVER_FILL = re.compile(
    r"gender|sex\b|race|ethnic|hispanic|latino|veteran|disab|sexual orientation|"
    r"pronoun.{0,4}optional|national origin|self.?identif|\beeo\b|"
    r"equal (employment|opportunity)|protected (class|category)|lgbt|"
    r"marital status|religio|citizenship status|date of birth|\bdob\b",
    re.I,
)

_RESUME_LABEL = re.compile(r"r[eé]sum[eé]|\bcv\b|curriculum vitae", re.I)
_COVER_LABEL = re.compile(r"cover.?letter", re.I)


def rules_payload() -> dict:
    """The matching rules as data, for the clients that can't import this module.

    The autofill brain runs in three places — here, the browser extension, and the
    iOS in-app browser — and the two JavaScript copies were hand-ported, so they
    drifted: both shipped an older, *narrower* EEO list than this file, meaning the
    phone would fill demographic questions the worker refuses. Serving the rules
    ends that: the clients fetch this and keep their bundled copy only as an
    offline fallback.

    Every pattern here is written in the subset of regex syntax that Python and
    JavaScript share (no named groups, no inline flags, no lookbehind), and
    ``tests/test_rules_parity.py`` runs the served rules through a real browser to
    prove both engines agree label-for-label.

    ``version`` changes whenever the rules do, so a client can cache on it.
    """
    payload = {
        "rules": [[key, pattern] for key, pattern in FIELD_RULES],
        "never_fill": _NEVER_FILL.pattern,
        "resume": _RESUME_LABEL.pattern,
        "cover": _COVER_LABEL.pattern,
        "flags": "i",
    }
    blob = json.dumps(payload, sort_keys=True).encode()
    payload["version"] = hashlib.sha256(blob).hexdigest()[:12]
    return payload


def is_eeo(label: str) -> bool:
    """True for demographic / EEO / self-identification labels, which we never fill
    on the user's behalf — as a *fact* or as a drafted answer. Public so every fill
    path (extension, worker filler, LLM agent) shares one definition."""
    return bool(_NEVER_FILL.search((label or "").strip().lower()))


def match_key(label: str) -> str | None:
    """The identity key a field's label maps to, or None. Returns None for
    demographic/EEO fields so they're never auto-filled."""
    text = (label or "").strip().lower()
    if not text or is_eeo(text):
        return None
    for key, rx in _COMPILED:
        if rx.search(text):
            return key
    return None


def select_value(options: list[str], value) -> str | None:
    """The option text that best matches ``value`` (exact → reverse-exact →
    contains), or None. Handles dropdowns and Yes/No groups."""
    v = str(value).strip().lower()
    if not v:
        return None
    opts = [(o, o.strip().lower()) for o in options if o and o.strip()]
    for o, ol in opts:           # exact
        if ol == v:
            return o
    for o, ol in opts:           # option contained in value, or vice-versa
        if v in ol or ol in v:
            return o
    return None


def option_for(label: str, options: list[str], identity: dict,
               key: str | None = None) -> tuple[str | None, str | None]:
    """``(identity key, option text)`` for a *choice* field.

    One decision point shared by all three choice controls the worker meets —
    native ``<select>``, custom ARIA combobox, and Yes/No radio group — so they
    can't drift apart. ``(None, None)`` when the label is unknown or EEO;
    ``(key, None)`` when the label is understood but the identity has no value or
    no option matches (the caller logs that as the skip reason).

    Pass ``key`` when the caller already resolved it from a richer signal than the
    label alone (the worker also consults a field's name/id).
    """
    key = key or match_key(label)
    if not key:
        return None, None
    value = (identity or {}).get(key)
    if value in (None, ""):
        return key, None
    return key, select_value(options, value)


def is_resume_field(label: str) -> bool:
    """True if a file-upload field is asking for a resume/CV (so the worker attaches
    the tailored resume there). False for cover-letter or other attachment fields,
    so we never put the resume in the wrong upload."""
    text = (label or "").strip().lower()
    if not text or _COVER_LABEL.search(text):
        return False
    return bool(_RESUME_LABEL.search(text))


def is_essay_label(label: str) -> bool:
    """True for free-text/essay prompts (worth a drafted answer) vs short facts.
    Mirrors the extension: long or question-shaped labels with no identity match.

    EEO prompts are excluded outright. Without that, a long demographic question
    ("Are you Hispanic or Latino?" plus its name/id) has no identity key, clears the
    length bar, and gets a *drafted answer* written into it — filling an EEO field
    through the back door."""
    text = (label or "").strip().lower()
    if not text or is_eeo(text) or match_key(text):
        return False
    return text.endswith("?") or len(text) > 40 or bool(
        re.search(r"\b(why|describe|tell us|cover letter|what (makes|interests)|"
                  r"in your own words|elaborate)\b", text)
    )
