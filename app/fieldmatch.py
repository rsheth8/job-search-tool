"""Shared application-form field matching.

The logic for "what does this form field mean, and what value goes in it" — one
source of truth for iOS Autofill via ``GET /apply/rules`` (served as JSON; the
app injects the JS matcher). Pure + dependency-free: a field's label text in, an
identity key out; a select's option texts + a desired value in, the best option out.
Fully testable; no DOM, no network.
"""
from __future__ import annotations

import hashlib
import json
import re

# (identity key, label regex). Served to iOS via /apply/rules.
# Order matters: more specific patterns first (email before address, preferred
# before first, location before city).
FIELD_RULES: list[tuple[str, str]] = [
    ("email", r"e-?mail"),
    ("preferred_name", r"preferred (first )?name|nick.?name|known as|goes by"),
    # "Name (First)" is as common on ATS forms as "First name", so match both orders.
    ("first_name", r"first.?name|given.?name|legal first|name\s*\(?\s*first|forename"),
    ("last_name", r"last.?name|family.?name|surname|name\s*\(?\s*last"),
    ("full_name", r"full.?name|^\s*name\s*$|your name|legal name"),
    ("pronouns", r"pronouns"),
    ("phone", r"\bphone\b|\bmobile\b|\btel(ephone)?\b|cell.?phone|contact number"),
    ("linkedin", r"linked.?in"),
    ("github", r"git.?hub"),
    ("portfolio", r"portfolio|personal (web)?site|^\s*website\s*$|^url$|other url|"
                  r"personal url|home ?page|personal page"),
    # Before location — "preferred work location" contains "location".
    # Don't use a bare "remote": it steals "authorized to work remotely".
    ("work_arrangement", r"preferred work (location|arrangement)|"
                         r"work (from home|arrangement|location preference)|"
                         r"on-?site|onsite|fully remote|remote or hybrid|"
                         r"hybrid or remote"),
    ("location", r"\blocation\b|where are you (based|located)|city.{0,5}state|"
                 r"where do you (live|reside)|currently (based|located|reside|living)|"
                 r"based in|city of residence"),
    ("address", r"street address|address line|mailing address|home address|"
                r"^\s*address\b|line 1"),
    ("city", r"\bcity\b|town"),
    ("country", r"\bcountry\b|nation"),
    ("state", r"\bstate\b|province|state.?/?\s*region"),
    ("zip", r"\bzip\b|postal code|post.?code"),
    ("school", r"school|university|college|institution|alma mater|where did you study|"
               r"name of (the )?school|educational institution"),
    ("degree", r"degree|qualification|level of (education|study)|highest (level of )?education|"
               r"degree (type|obtained|earned)"),
    ("discipline", r"major|discipline|field of study|concentration|area of study|"
                   r"what did you (study|major)"),
    ("gpa", r"\bgpa\b|grade point|cumulative gpa|overall gpa"),
    ("grad_month", r"end date month|grad(?:uation)? month"),
    ("grad_year_num", r"end date year"),
    ("grad_year", r"when (do|will) you graduate|expected graduation|"
                  r"grad(uation)?.{0,12}(year|date)|class of|completion (year|date)|"
                  r"year of grad|anticipated graduation|graduation date"),
    ("intern_season", r"winter or summer internship|prefer.{0,30}internship|"
                      r"internship.{0,16}(term|season|preference|period|availability)|"
                      r"which (term|season|internship)"),
    ("current_company", r"current (employer|company)|present (employer|company)|"
                        r"where do you (currently )?work|most recent (employer|company)|"
                        r"current or most recent employer|^\s*employer\s*$"),
    ("current_title", r"current (title|role|position)|present (title|role|position)|"
                      r"most recent (title|role|position)|job title"),
    ("years_experience", r"years.{0,16}experience|experience.{0,16}years|\byoe\b|"
                         r"how many years"),
    ("salary_expectation", r"salary (expectation|requirement)|expected (salary|compensation|pay)|"
                           r"desired (salary|pay|compensation)|compensation expectation|"
                           r"pay expectation|target (salary|comp|compensation)"),
    ("start_date", r"start date|available to start|earliest (start|availability)|"
                   r"when (can|could|are) you (start|available)|date available|notice period|"
                   r"availability date"),
    ("willing_to_relocate", r"willing to relocate|open to relocat|able to relocate|relocat"),
    ("work_authorized", r"authori[sz]ed to work|work authori[sz]ation|legally.{0,16}work|"
                        r"eligible to work|right to work|work eligibility"),
    ("needs_sponsorship", r"sponsor(ship)?|require.{0,20}visa|visa.{0,16}status|"
                          r"immigration status|now or in the future.{0,24}sponsor"),
    ("background_check", r"background check|criminal (background|history|record)|background screening"),
    ("drug_test", r"drug (test|screen|screening)|substance (test|screen)"),
    ("over_18", r"over 18|18 years|at least 18|age 18|legal age|18 years of age"),
    ("can_travel", r"willing to travel|able to travel|travel (required|for (work|this))|"
                   r"open to travel|willing and able to travel"),
    ("previously_applied", r"previously applied|applied (here|before|to (this|us))|"
                           r"worked (here|for us|at this)|former employee|prior application|"
                           r"previously (been )?employed|ever (worked for|applied to)"),
    # Before related_to_employee — a bare "referral" used to steal "referral source".
    ("how_heard", r"how did you (hear|learn|find)|where did you (hear|learn|find)|"
                  r"hear about (this|us|the)|referral source|source of (this )?application|"
                  r"how.?['’]?d you (find|hear)|find this (role|job|opportunit)"),
    ("related_to_employee", r"related to|relative (at|of)|relatives? who work|know anyone|"
                            r"family member|referred by|employee of"),
    # Optional EEO — only filled when the identity has a value saved.
    # "gender identity" is hard-blocked by never_fill before these run.
    ("gender", r"\bgender\b|^\s*sex\s*$|what is your sex\b"),
    # Avoid a bare "/" so JS regex literals in client fallbacks stay valid.
    ("race", r"\brace\b|race\s*/?\s*ethnicity|racial identity"),
    ("ethnicity", r"^\s*ethnicity\s*$|ethnic background"),
    ("hispanic_latino", r"hispanic|latino"),
    ("veteran_status", r"veteran|protected veteran|military status"),
    ("disability_status", r"disabilit(y|ies)|disabled"),
]
_COMPILED = [(key, re.compile(pat, re.I)) for key, pat in FIELD_RULES]

# Gender/race/veteran/disability/Hispanic-Latino are in FIELD_RULES (opt-in when
# the identity has a value). Orientation, religion, DOB, … stay never-filled.
_NEVER_FILL = re.compile(
    r"sexual orientation|pronoun.{0,4}optional|national origin|self.?identif|\beeo\b|"
    r"equal (employment|opportunity)|protected (class|category)|lgbt|"
    r"marital status|religio|citizenship status|date of birth|\bdob\b|"
    r"transgender|lgbtq|gender identity|birth(day|date)|\bbday\b",
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
        "attr_rules": [[key, pattern] for key, pattern in ATTR_RULES],
        "autocomplete": dict(AUTOCOMPLETE),
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


# Stable ATS name/id tokens. Companies rewrite the visible label ("Applicant",
# "Reach us at"); Greenhouse/Lever/Ashby keep job_application[first_name],
# _systemfield_email, autocomplete="given-name". Simplify's reliability is
# mostly this signal, not a giant synonym list.
ATTR_RULES: list[tuple[str, str]] = [
    ("linkedin", r"urls\]\[linkedin|linkedin_url|(?:^|\[)linkedin(?:\]|$)"),
    ("github", r"urls\]\[github|github_url|(?:^|\[)github(?:\]|$)"),
    ("portfolio", r"urls\]\[(?:portfolio|website|other)|personal_url|website_url"),
    ("email", r"job_application\[email\]|_systemfield_email|(?:^|\[)email(?:\]|$)"),
    ("preferred_name", r"preferred_first_name|preferred_name"),
    ("first_name", r"first_name|_systemfield_first"),
    ("last_name", r"last_name|_systemfield_last"),
    ("phone", r"job_application\[phone\]|_systemfield_phone|(?:^|\[)phone(?:\]|$)"),
    ("location", r"job_application\[location\]|_systemfield_location|(?:^|\[)location(?:\]|$)"),
    ("school", r"school_name|_systemfield_school|educations?[^\s]*school"),
    ("degree", r"educations?[^\s]*degree"),
    ("discipline", r"educations?[^\s]*discipline"),
    ("grad_month", r"end_date\]\[month"),
    ("grad_year_num", r"end_date\]\[year"),
    ("current_company", r"company_name|_systemfield_company|(?:^|\[)org(?:\]|$)"),
    ("current_title", r"employments?[^\s]*title"),
    ("full_name", r"_systemfield_name$|^name$"),
    ("address", r"street_address|address_line|job_application\[address\]"),
    ("city", r"(?:^|\[)city(?:\]|$)|_systemfield_city"),
    ("state", r"(?:^|\[)state(?:\]|$)|_systemfield_state"),
    ("zip", r"postal_code|(?:^|\[)zip(?:\]|$)|_systemfield_zip"),
    ("country", r"job_application\[country\]|_systemfield_country|(?:^|\[)country(?:\]|$)"),
]
_ATTR_COMPILED = [(key, re.compile(pat, re.I)) for key, pat in ATTR_RULES]

# WHATWG autocomplete tokens. "bday" / credit-card / password are omitted on purpose.
AUTOCOMPLETE: dict[str, str] = {
    "given-name": "first_name",
    "family-name": "last_name",
    "name": "full_name",
    "nickname": "preferred_name",
    "email": "email",
    "tel": "phone",
    "tel-national": "phone",
    "street-address": "address",
    "address-line1": "address",
    "address-level2": "city",
    "address-level1": "state",
    "postal-code": "zip",
    "country": "country",
    "country-name": "country",
    "organization": "current_company",
    "organization-title": "current_title",
    "url": "portfolio",
}


def match_attr(name: str = "", html_id: str = "") -> str | None:
    """Identity key from an input's name/id, or None."""
    for source in (name, html_id):
        s = (source or "").strip()
        if not s:
            continue
        if is_eeo(s):
            return None
        for key, rx in _ATTR_COMPILED:
            if rx.search(s):
                return key
    return None


def match_autocomplete(value: str) -> str | None:
    """Identity key from a WHATWG autocomplete token, or None."""
    if not value:
        return None
    token = value.strip().lower().split()[-1]
    if token in ("off", "on", "new-password", "current-password"):
        return None
    return AUTOCOMPLETE.get(token)


def match_field(label: str, *, name: str = "", html_id: str = "",
                autocomplete: str = "", input_type: str = "") -> str | None:
    """Label first (visible intent), then autocomplete, name/id, then input type.

    Hard-blocked EEO on any of those signals wins. This is the same order the
    iOS filler uses — tests/test_autofill_corpus.py pins it against gold forms
    whose labels were written *not* to match FIELD_RULES.
    """
    if is_eeo(label) or is_eeo(name) or is_eeo(html_id):
        return None
    keyed = match_key(label)
    if keyed:
        return keyed
    ac = match_autocomplete(autocomplete)
    if ac:
        return ac
    attr = match_attr(name, html_id)
    if attr:
        return attr
    t = (input_type or "").strip().lower()
    if t == "email":
        return "email"
    if t == "tel":
        return "phone"
    return None


# ATS typeaheads and <select>s almost never store the identity string verbatim —
# "B.S." vs "Bachelor's Degree", "Chicago, IL" vs "Chicago, Illinois, United States",
# "University of Minnesota Twin Cities" vs the hyphenated campus name. Autofill has
# to pick the closest *allowed* option; typing a value that isn't in the list looks
# filled and then fails validation on save.
_PLACEHOLDER_OPT = re.compile(
    r"^(select|choose|pick|search|type|start typing|please select|n/?a|--+|—+)?\.?$",
    re.I,
)
_STOP_OPT = {
    "the", "of", "and", "a", "an", "at", "to", "for", "or", "on",
}
_GENERIC_OPT = {
    "university", "college", "school", "united", "states", "city", "campus",
    "institute", "department", "degree", "option",
}
_STATE_ABBR = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas",
    "ca": "california", "co": "colorado", "ct": "connecticut", "de": "delaware",
    "fl": "florida", "ga": "georgia", "hi": "hawaii", "id": "idaho",
    "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas",
    "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada",
    "nh": "new hampshire", "nj": "new jersey", "nm": "new mexico", "ny": "new york",
    "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
    "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina",
    "sd": "south dakota", "tn": "tennessee", "tx": "texas", "ut": "utah",
    "vt": "vermont", "va": "virginia", "wa": "washington", "wv": "west virginia",
    "wi": "wisconsin", "wy": "wyoming", "dc": "district of columbia",
}
# English words that are also USPS codes — only expand them from ", XX" location
# tails, never from running text ("in progress" must not become Indiana).
_AMBIGUOUS_ABBR = {"in", "or", "me", "hi", "oh", "ok", "id", "la", "ma", "md"}
_LEVEL_ORDER = ("doctorate", "master", "bachelor", "associate", "high school")
_COMMA_ABBR = re.compile(r",\s*([A-Za-z]{2})\b")


def _canonical_option(text: str, *, expand_states: bool = False) -> str:
    t = (text or "").lower().strip()
    if not t:
        return ""
    t = t.replace("&", " and ").replace("-", " ")
    t = re.sub(r"\bb\.?\s*sc?\b", " bachelor ", t)
    t = re.sub(r"\bb\.?\s*a\.?\b", " bachelor ", t)
    t = re.sub(r"\bb\.?\s*f\.?\s*a\.?\b", " bachelor ", t)
    t = re.sub(r"\bm\.?\s*sc?\b", " master ", t)
    t = re.sub(r"\bm\.?\s*a\.?\b", " master ", t)
    t = re.sub(r"\bmba\b", " mba master ", t)
    t = re.sub(r"\bph\.?\s*d\.?\b", " doctorate ", t)
    t = re.sub(r"\bbachelors?\b", " bachelor ", t)
    t = re.sub(r"\bmasters?\b", " master ", t)
    t = re.sub(r"\b(doctorate|doctoral|dphil)\b", " doctorate ", t)
    t = re.sub(r"\bassociates?\b", " associate ", t)
    t = re.sub(r"\b(women|woman)\b", " female ", t)
    t = re.sub(r"\b(men|man)\b", " male ", t)
    t = re.sub(r"\bnon\s*binary\b", " nonbinary ", t)
    t = re.sub(r"\bwfh\b", " remote ", t)
    t = re.sub(r"\bwork from home\b", " remote ", t)
    t = re.sub(r"\bin office\b", " onsite ", t)
    t = re.sub(r"\bon site\b", " onsite ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    out: list[str] = []
    for tok in t.split():
        if tok in ("usa", "us"):
            out.extend(["united", "states"])
        elif tok == "uk":
            out.extend(["united", "kingdom"])
        elif expand_states and tok in _STATE_ABBR and tok not in _AMBIGUOUS_ABBR:
            out.extend(_STATE_ABBR[tok].split())
        elif tok not in _STOP_OPT:
            out.append(tok)
    return " ".join(out)


_STATE_NAMES = frozenset(_canonical_option(v) for v in _STATE_ABBR.values())


def _expand_bare_state(raw: str, options: list[str]) -> str:
    """``IN`` → ``Indiana``, but only when the list really is state names.

    A two-letter value that *is* a USPS code is unambiguous, unlike the same
    letters inside running text ("in progress" must not become Indiana) — so the
    ``_AMBIGUOUS_ABBR`` guard, which exists for running text, must not apply
    here. Without this the state select got the *wrong state*: ``IN`` scored
    highest against "Maine" and ``LA`` against "Alabama", while ``OR``, ``MA``,
    ``MD`` and ``ME`` matched nothing at all and were left blank.
    """
    code = raw.strip().lower()
    if len(code) != 2 or not code.isalpha() or code not in _STATE_ABBR:
        return raw
    if any(_canonical_option(o) in _STATE_NAMES for o in options):
        return _STATE_ABBR[code].title()
    return raw


def _levels_in(canonical: str) -> tuple[str, ...]:
    found = []
    for level in _LEVEL_ORDER:
        if level == "high school":
            if "high" in canonical.split() and "school" in canonical.split():
                found.append(level)
        elif level in canonical.split() or (level == "master" and "mba" in canonical.split()):
            found.append(level)
    return tuple(found)


def _education_pick(options: list[str], want: str) -> str | None:
    ranked: list[tuple[str, str]] = []
    for o in options:
        lv = _levels_in(_canonical_option(o))
        if lv:
            ranked.append((o, lv[0]))
    if len(ranked) < 2:
        return None
    val_levels = _levels_in(want)
    if not val_levels:
        return None
    highest = val_levels[0]
    for o, lv in ranked:
        if lv == highest:
            return o
    return None


_GPA_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*[-–—to]+\s*(\d+(?:\.\d+)?)", re.I)
_GPA_UNDER = re.compile(r"(\d+(?:\.\d+)?)\s*or\s*(?:under|below|less)", re.I)
_MONTH_NUM = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


def _gpa_range_pick(options: list[str], raw: str) -> str | None:
    """3.5 → '3.1 - 3.6' on banded GPA dropdowns (Verkada and similar)."""
    m = re.search(r"\b([0-4](?:\.\d+)?)\b", raw)
    if not m:
        return None
    gpa = float(m.group(1))
    banded = [o for o in options if _GPA_RANGE.search(o) or _GPA_UNDER.search(o)]
    if len(banded) < 2:
        return None
    hits: list[tuple[str, float]] = []
    for o in banded:
        rm = _GPA_RANGE.search(o)
        if rm:
            lo, hi = float(rm.group(1)), float(rm.group(2))
            if min(lo, hi) - 1e-9 <= gpa <= max(lo, hi) + 1e-9:
                hits.append((o, abs(hi - lo)))
            continue
        um = _GPA_UNDER.search(o)
        if um and gpa <= float(um.group(1)) + 1e-9:
            hits.append((o, 99.0))
    if not hits:
        return None
    hits.sort(key=lambda x: x[1])
    return hits[0][0]


def _parse_months_year(text: str) -> tuple[list[int], int | None]:
    year_m = re.search(r"\b((?:19|20)\d{2})\b", text)
    year = int(year_m.group(1)) if year_m else None
    months = []
    for name, num in _MONTH_NUM.items():
        if re.search(rf"\b{name}\b", text, re.I):
            months.append(num)
    return sorted(set(months)), year


def _date_bucket_pick(options: list[str], raw: str) -> str | None:
    """December 2027 → 'Sept - Dec 2027'; also maps a month name onto January–December."""
    want_months, want_year = _parse_months_year(raw)
    if not want_months and not want_year:
        return None
    scored: list[tuple[str, int]] = []
    bucketish = 0
    for o in options:
        om, oy = _parse_months_year(o)
        if oy is None and len(om) < 1:
            continue
        if oy is not None or len(om) >= 2:
            bucketish += 1
        if want_year and oy and want_year != oy:
            continue
        if want_months and om:
            lo, hi = min(om), max(om)
            if any(lo <= m <= hi for m in want_months):
                scored.append((o, abs((lo + hi) / 2 - want_months[0])))
        elif want_months and len(om) == 1 and om[0] in want_months:
            scored.append((o, 0))
        elif want_year and oy == want_year and not want_months:
            scored.append((o, 0))
    # Month-only dropdowns (January…December) aren't "buckets" but still count.
    month_only = all(len(_parse_months_year(o)[0]) == 1 and _parse_months_year(o)[1] is None
                     for o in options if o)
    if not scored:
        return None
    if bucketish < 2 and not month_only:
        return None
    scored.sort(key=lambda x: x[1])
    return scored[0][0]


def _year_pick(options: list[str], raw: str) -> str | None:
    years = re.findall(r"\b(?:19|20)\d{2}\b", raw)
    if not years:
        return None
    year_opts = [o.strip() for o in options if re.fullmatch(r"(?:19|20)\d{2}", o.strip())]
    if len(year_opts) < 3:
        return None
    want = years[-1]
    for o in year_opts:
        if o == want:
            return o
    return None


_DECLINE_OPT = re.compile(
    r"decline|prefer not|do not wish|don't wish|choose not to|rather not",
    re.I,
)
# An option that *opens* with Yes or No has already declared which side it is on.
# Reading the rest of it for shape flips the answer on the single most common
# work-authorization phrasing there is: "Yes, I do not require sponsorship" hits
# ``\bnot\b`` and classifies as No, so both options on that question come back
# "no", no side has a candidate, and the field is left blank on every Greenhouse
# and Lever form that words it that way. The lead wins.
_LEAD_YES = re.compile(r"^\s*(?:yes|y)\b", re.I)
_LEAD_NO = re.compile(r"^\s*(?:no|n)\b", re.I)
_NO_SHAPE = re.compile(
    r"\b(?:not|never|none)\b|(?:^|[\s,])no(?:[\s,]|$)|i am not|i do not|"
    r"do not have|don't have|not hispanic|not latino",
    re.I,
)
_YES_SHAPE = re.compile(
    r"(?:^|[\s,])yes(?:[\s,]|$)|^\s*y\s*$|\bi am\b|\bauthorized\b|\bwilling\b|"
    r"\bable to\b|\bopen to\b|\bhispanic\b|\blatino\b|\bveteran\b|\bdisabilit",
    re.I,
)
_RANGE_NUM = re.compile(
    r"([$]?\s*\d[\d,]*(?:\.\d+)?)\s*[-–—]\s*([$]?\s*\d[\d,]*(?:\.\d+)?)",
)
_RANGE_TO = re.compile(
    r"([$]?\s*\d[\d,]*(?:\.\d+)?)\s+to\s+([$]?\s*\d[\d,]*(?:\.\d+)?)",
    re.I,
)
_PLUS_NUM = re.compile(r"([$]?\s*\d[\d,]*(?:\.\d+)?)\s*\+")
_UNDER_NUM = re.compile(
    r"(?:under|below|less than|<\s*)\s*([$]?\s*\d[\d,]*(?:\.\d+)?)",
    re.I,
)


def _as_yes_no(raw: str) -> str | None:
    t = (raw or "").strip().lower()
    if t in ("yes", "y", "true", "1"):
        return "yes"
    if t in ("no", "n", "false", "0"):
        return "no"
    if re.search(r"^\s*yes\b", t) and not re.search(r"\bno\b", t):
        return "yes"
    if re.search(r"^\s*no\b", t):
        return "no"
    if re.search(r"\b(not|never)\b", t) and not re.search(r"\byes\b", t):
        return "no"
    return None


def _option_yes_no(text: str) -> str | None:
    t = (text or "").strip()
    if not t or _DECLINE_OPT.search(t):
        return None
    if _LEAD_YES.search(t):
        return "yes"
    if _LEAD_NO.search(t):
        return "no"
    if _NO_SHAPE.search(t):
        return "no"
    if _YES_SHAPE.search(t) or t.lower() in ("yes", "y", "true"):
        return "yes"
    if t.lower() in ("no", "n", "false"):
        return "no"
    return None


def _yes_no_pick(options: list[str], raw: str) -> str | None:
    """Yes/No identity values onto long ATS options ('I am authorized…')."""
    want = _as_yes_no(raw)
    if want is None:
        return None
    yeses, nos = [], []
    for o in options:
        kind = _option_yes_no(o)
        if kind == "yes":
            yeses.append(o)
        elif kind == "no":
            nos.append(o)
    if not yeses or not nos:
        return None
    pool = yeses if want == "yes" else nos
    return pool[0]


def _parse_moneyish(s: str) -> float | None:
    s = (s or "").replace(",", "").replace("$", "").strip()
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    return float(m.group(0))


def _value_number(raw: str) -> float | None:
    t = (raw or "").replace(",", "").replace("$", "")
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", t)]
    if not nums:
        return None
    return max(nums)


def _option_span(text: str) -> tuple[float, float] | None:
    for rx in (_RANGE_NUM, _RANGE_TO):
        m = rx.search(text)
        if m:
            lo, hi = _parse_moneyish(m.group(1)), _parse_moneyish(m.group(2))
            if lo is None or hi is None:
                continue
            return (min(lo, hi), max(lo, hi))
    m = _PLUS_NUM.search(text)
    if m:
        lo = _parse_moneyish(m.group(1))
        if lo is not None:
            return (lo, float("inf"))
    m = _UNDER_NUM.search(text)
    if m:
        hi = _parse_moneyish(m.group(1))
        if hi is not None:
            return (0.0, hi)
    return None


def _numeric_bucket_pick(options: list[str], raw: str) -> str | None:
    """2 → '1–3 years'; 120000 → '$100,000 – $130,000'; 5 → '5+'."""
    want = _value_number(raw)
    if want is None:
        return None
    spanned: list[tuple[str, tuple[float, float]]] = []
    for o in options:
        sp = _option_span(o)
        if sp:
            spanned.append((o, sp))
    if len(spanned) < 2:
        return None
    hits: list[tuple[str, float, float]] = []
    for o, (lo, hi) in spanned:
        if lo - 1e-9 <= want <= hi + 1e-9:
            width = 1e12 if hi == float("inf") else (hi - lo)
            hits.append((o, width, lo))
    if not hits:
        return None
    # Tightest span; among open-ended "3+" / "5+" pick the highest floor.
    hits.sort(key=lambda x: (x[1], -x[2]))
    return hits[0][0]


def _option_score(want_c: str, opt_c: str) -> float:
    if not want_c or not opt_c:
        return 0.0
    wt, ot = set(want_c.split()), set(opt_c.split())
    if not wt or not ot:
        return 0.0

    def _neg(tokens: set[str]) -> bool:
        return bool(tokens & {"not", "no", "never", "none"})

    # "Authorized" must not land on "Not authorized" just because it contains the word.
    if _neg(wt) != _neg(ot):
        return 0.0
    if want_c == opt_c:
        return 10.0
    if want_c in opt_c or opt_c in want_c:
        return 3.0 + min(len(want_c), len(opt_c)) / max(len(want_c), len(opt_c))
    distinct = [t for t in want_c.split() if t not in _GENERIC_OPT and len(t) > 2]
    if distinct and distinct[0] not in ot:
        return 0.0
    shared = wt & ot
    if not shared:
        return 0.0
    return (len(shared) / len(wt)) + (len(shared) / len(ot))


def select_value(options: list[str], value) -> str | None:
    """The allowed option that best matches ``value``, or None.

    ATS dropdowns and typeaheads only persist a value that's on their list.
    Exact text is tried first, then aliases (USA → United States, B.S. → Bachelor's,
    IL → Illinois) and token overlap so 'Chicago, IL' can select
    'Chicago, Illinois, United States'.
    """
    raw = str(value).strip()
    if not raw:
        return None
    cleaned = []
    for o in options:
        text = str(o).strip() if o is not None else ""
        if not text or _PLACEHOLDER_OPT.match(text):
            continue
        cleaned.append(text)
    if not cleaned:
        return None

    # An option that *is* the value wins before any heuristic. Canonicalizing
    # first can destroy a short value outright — "OR" (Oregon) and "IN"
    # (Indiana) are stop words, so they normalized to the empty string and
    # matched nothing.
    for o in cleaned:
        if o.strip().lower() == raw.lower():
            return o

    raw = _expand_bare_state(raw, cleaned)
    expand = bool(_COMMA_ABBR.search(raw) or len(raw) == 2
                  or any(_COMMA_ABBR.search(o) for o in cleaned))
    want = _canonical_option(raw, expand_states=expand)
    if not want:
        return None

    edu = _education_pick(cleaned, want)
    if edu:
        return edu
    gpa = _gpa_range_pick(cleaned, raw)
    if gpa:
        return gpa
    bucket = _date_bucket_pick(cleaned, raw)
    if bucket:
        return bucket
    year = _year_pick(cleaned, raw)
    if year:
        return year
    yn = _yes_no_pick(cleaned, raw)
    if yn:
        return yn
    bucket_n = _numeric_bucket_pick(cleaned, raw)
    if bucket_n:
        return bucket_n

    best, best_score = None, 0.0
    for o in cleaned:
        oc = _canonical_option(o, expand_states=expand)
        if not oc:
            continue
        if oc == want:
            return o
        score = _option_score(want, oc)
        if score > best_score:
            best, best_score = o, score
    if best is not None and best_score >= 0.5:
        return best
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
