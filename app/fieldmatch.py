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
    ("full_name", r"full.?name|^\s*name\s*$|your name|legal name"),
    ("pronouns", r"pronouns"),
    ("phone", r"phone|mobile|tel(ephone)?|contact number"),
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
    ("country", r"\bcountry\b|nation"),
    ("school", r"school|university|college|institution|alma mater|where did you study"),
    ("degree", r"degree|qualification|level of (education|study)"),
    ("discipline", r"major|discipline|field of study|concentration"),
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
    ("willing_to_relocate", r"willing to relocate|open to relocat|able to relocate|relocat"),
    ("work_authorized", r"authori[sz]ed to work|work authori[sz]ation|legally.{0,12}work|eligible to work|right to work"),
    ("needs_sponsorship", r"sponsor(ship)?|require.{0,12}visa|visa.{0,12}status|immigration status"),
    ("background_check", r"background check|criminal (background|history|record)|background screening"),
    ("drug_test", r"drug (test|screen|screening)|substance (test|screen)"),
    ("over_18", r"over 18|18 years|at least 18|age 18|legal age"),
    ("can_travel", r"willing to travel|able to travel|travel (required|for (work|this))|open to travel"),
    ("previously_applied", r"previously applied|applied (here|before|to (this|us))|worked (here|for us|at this)|former employee|prior application"),
    ("related_to_employee", r"related to|relative (at|of)|know anyone|family member|referral|employee of"),
    ("work_arrangement", r"remote|hybrid|on-?site|onsite|work (from home|arrangement|location preference)"),
    ("how_heard", r"how did you (hear|learn|find)|where did you hear|referral source|source of (this )?application"),
    # Optional EEO — only filled when the identity has a value saved.
    ("gender", r"^\s*gender\s*$|^\s*sex\s*$"),
    # Avoid a bare "/" so JS regex literals in client fallbacks stay valid.
    ("race", r"^\s*race\s*$|race\s*/?\s*ethnicity|racial identity"),
    ("ethnicity", r"^\s*ethnicity\s*$|ethnic background"),
    ("veteran_status", r"veteran|protected veteran|military status"),
    ("disability_status", r"disabilit(y|ies)|disabled"),
]
_COMPILED = [(key, re.compile(pat, re.I)) for key, pat in FIELD_RULES]

# Hard-blocked topics — never auto-fill these even if a value exists.
# Gender/race/veteran/disability are in FIELD_RULES (opt-in when identity has them).
_NEVER_FILL = re.compile(
    r"sexual orientation|pronoun.{0,4}optional|national origin|self.?identif|\beeo\b|"
    r"equal (employment|opportunity)|protected (class|category)|lgbt|"
    r"marital status|religio|citizenship status|date of birth|\bdob\b|"
    r"transgender|lgbtq|hispanic|latino|gender identity",
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
    """True for sensitive self-ID labels we never fill (orientation, religion, DOB,
    …). Gender/race/veteran/disability are *not* included here — those map via
    FIELD_RULES and only fill when the identity has a value."""
    return bool(_NEVER_FILL.search((label or "").strip().lower()))


def match_key(label: str) -> str | None:
    """The identity key a field's label maps to, or None. Returns None for
    hard-blocked EEO topics (orientation, religion, DOB, …)."""
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
