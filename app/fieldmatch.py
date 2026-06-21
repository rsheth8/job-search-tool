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

import re

# (identity key, label regex) — kept in lockstep with extension/content.js RULES.
# Order matters: more specific patterns first (email before address, preferred
# before first, location before city).
FIELD_RULES: list[tuple[str, str]] = [
    ("email", r"e-?mail"),
    ("preferred_name", r"preferred (first )?name|nick.?name|known as|goes by"),
    ("first_name", r"first.?name|given.?name|legal first"),
    ("last_name", r"last.?name|family.?name|surname"),
    ("full_name", r"full.?name|^\s*name\s*$|your name|legal name"),
    ("pronouns", r"pronouns"),
    ("phone", r"phone|mobile|tel(ephone)?"),
    ("linkedin", r"linked.?in"),
    ("github", r"git.?hub"),
    ("portfolio", r"portfolio|personal (web)?site|^\s*website\s*$|^url$|other url|personal url"),
    ("location", r"\blocation\b|where are you (based|located)|city.{0,5}state"),
    ("address", r"street address|address line|mailing address|home address|^\s*address\b"),
    ("city", r"\bcity\b|town"),
    ("state", r"\bstate\b|province|region"),
    ("zip", r"\bzip\b|postal code|post.?code"),
    ("country", r"\bcountry\b|nation"),
    ("school", r"school|university|college|institution|alma mater"),
    ("degree", r"degree|qualification|level of (education|study)"),
    ("discipline", r"major|discipline|field of study|concentration"),
    ("gpa", r"\bgpa\b|grade point"),
    ("grad_year", r"grad(uation)?.{0,8}(year|date)|class of|completion (year|date)"),
    ("current_company", r"current (employer|company)|present (employer|company)|where do you (currently )?work"),
    ("current_title", r"current (title|role|position)|present (title|role|position)"),
    ("years_experience", r"years.{0,10}experience|experience.{0,10}years|\byoe\b"),
    ("salary_expectation", r"salary (expectation|requirement)|expected (salary|compensation|pay)|desired (salary|pay|compensation)|compensation expectation|pay expectation"),
    ("start_date", r"start date|available to start|earliest (start|availability)|when can you start|date available"),
    ("willing_to_relocate", r"willing to relocate|open to relocat|able to relocate|relocat"),
    ("work_authorized", r"authori[sz]ed to work|work authori[sz]ation|legally.{0,12}work|eligible to work|right to work"),
    ("needs_sponsorship", r"sponsor(ship)?|require.{0,12}visa|visa.{0,12}status|immigration status"),
]
_COMPILED = [(key, re.compile(pat, re.I)) for key, pat in FIELD_RULES]

# Demographic / EEO fields we NEVER auto-fill (sensitive — left to the human).
_NEVER_FILL = re.compile(
    r"gender|sex\b|race|ethnic|hispanic|latino|veteran|disab|sexual orientation|"
    r"pronoun.{0,4}optional", re.I,
)


def match_key(label: str) -> str | None:
    """The identity key a field's label maps to, or None. Returns None for
    demographic/EEO fields so they're never auto-filled."""
    text = (label or "").strip().lower()
    if not text or _NEVER_FILL.search(text):
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


def is_essay_label(label: str) -> bool:
    """True for free-text/essay prompts (worth a drafted answer) vs short facts.
    Mirrors the extension: long or question-shaped labels with no identity match."""
    text = (label or "").strip().lower()
    if not text or match_key(text):
        return False
    return text.endswith("?") or len(text) > 40 or bool(
        re.search(r"\b(why|describe|tell us|cover letter|what (makes|interests)|"
                  r"in your own words|elaborate)\b", text)
    )
