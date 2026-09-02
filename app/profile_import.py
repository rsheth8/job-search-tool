"""Fill a profile from a resume, a GitHub user, or a LinkedIn URL.

Imports only write *empty* fields so a quiz skip or a later edit is never
overwritten. LinkedIn pages are not scraped (blocked + against their terms);
a profile URL is stored, and a LinkedIn PDF goes through the resume parser.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from io import BytesIO

from . import applicant, knowledge, profile
from .config import get_settings

logger = logging.getLogger("profile_import")

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_PARSE_CHARS = 24_000
MAX_KNOWLEDGE = 12

_EEO_KEYS = frozenset({
    "gender", "race", "ethnicity", "veteran_status", "disability_status",
})
_BOOL_KEYS = frozenset(applicant.BOOL_FIELDS)

_PROFILE_KEYS = ("roles", "keywords", "locations", "seniority", "resume_summary")

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.I)
_PHONE_RE = re.compile(
    r"(?:\+?1[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}\b"
)
# Country subdomains (uk.linkedin.com) and m/mwlite paths show up when people
# paste a profile from the mobile app. Query strings are ignored.
_LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:(?:www|m|[\w-]+)\.)?linkedin\.com/"
    r"(?:in|pub|mwlite/in)/([A-Za-z0-9\-_%]+)",
    re.I,
)
_GITHUB_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/"
    r"([A-Za-z0-9](?:[A-Za-z0-9\-]{0,37}[A-Za-z0-9])?)\b",
    re.I,
)
#: Real USPS codes. Without this, "([A-Z]{2})" accepts any two capitals, and a
#: resume that mentions "workshops on React, AI/ML" yields city="React",
#: state="AI" -- which is then typed into City and State on real applications.
#: Every other guard in this module (`_SKILLISH`, `_is_geo_location`) protects
#: the `locations` search field; identity city/state was taken straight from
#: this regex, ungated.
_US_STATES = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY",
    "DC", "PR", "VI", "GU", "AS", "MP",          # federal district + territories
    "AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK",
    "YT",                                         # Canadian provinces
})

_CITY_STATE_RE = re.compile(
    # The separator must not cross a newline. `\s` did, so a resume with the
    # name directly above the "City, ST" line -- the commonest header there is --
    # parsed as city="Rahil Sheth\nChicago", which then became `location` and got
    # typed into City on real applications.
    #
    # The trailing boundary is `(?![a-z])`, not `\b`. PDF text extraction runs a
    # right-aligned date into the location it sits beside -- a real resume
    # produced "Minneapolis, MNAug 2023 - Present" -- and `\b` finds no boundary
    # between "MN" and "Aug", so the only genuine location in the document was
    # missed while a false one further down was taken. Requiring the next
    # character not to be lowercase accepts "MNAug" and still rejects
    # "MNesota"; the state whitelist above is what makes that safe.
    r"\b([A-Z][a-z]+(?:[ \t\-][A-Z][a-z]+)*),[ \t]*([A-Z]{2})(?![a-z])"
)
def _find_city_state(text: str) -> tuple[str, str] | None:
    """First "City, ST" whose ST is a real state code. None if there isn't one.

    Scanning for the *first valid* match rather than the first match is the
    point: a resume that mentions a skill pair before it mentions where the
    person lives would otherwise hand the skill pair to City and State.
    """
    for m in _CITY_STATE_RE.finditer(text or ""):
        if m.group(2).upper() in _US_STATES:
            return m.group(1), m.group(2).upper()
    return None


_GRAD_YEAR_RE = re.compile(r"\b(20[1-3]\d)\b")
#: "Aug 2023 - Present". The year is when they *started*, and the line is
#: explicitly saying they have not finished, so it is not a graduation year.
_ONGOING_RE = re.compile(
    r"\b(20[1-3]\d)\s*(?:[-\u2010-\u2015]|\bto\b|\buntil\b)\s*"
    r"(?:present|current|now|ongoing)\b",
    re.I,
)
#: Years stated as the end of a degree, in descending order of how much the
#: resume is actually promising. Anything here beats counting years.
_GRAD_LABELLED = (
    r"expected\s+(?:[A-Za-z]+\s+)?(20[1-3]\d)",
    r"(?:graduat(?:es|ing|ion)|class of|degree conferred)"
    r"[^\n0-9]{0,16}(20[1-3]\d)",
)
_DEGREE_RE = re.compile(
    r"\b((?:B\.?S\.?|B\.?A\.?|M\.?S\.?|M\.?Eng\.?|Ph\.?D\.?|MBA|"
    r"Bachelor(?:'s)?(?: of [^,\n]+)?|Master(?:'s)?(?: of [^,\n]+)?)"
    r"(?:\s+(?:of\s+)?(?:Science|Arts|Engineering|Computer Science))?)",
    re.I,
)
#: One capitalised word of an institution name.
_NAME_WORD = r"[A-Z][A-Za-z.'\-]+"
#: The rest of a name. Two fixes over a plain ``(?:\s+[A-Z]\w+)`` repeat:
#:
#: * lowercase connectives are allowed *between* capitalised words, so
#:   "University of Illinois at Urbana-Champaign" no longer stops dead at "at".
#:   A capitalised word must follow, so a trailing preposition can't end a name.
#: * ``[ \t]`` instead of ``\s`` -- ``\s`` crossed newlines, so the section
#:   header above the name joined it ("EDUCATION\nUniversity") and won the
#:   alternation, losing the real name entirely. Same bug class as the
#:   city/state header regex above.
_NAME_TAIL = (rf"(?:[ \t]+(?:at|of|in|and|the)[ \t]+{_NAME_WORD}"
              rf"|[ \t]+{_NAME_WORD})")

_SCHOOL_RE = re.compile(
    r"\b("
    rf"University of {_NAME_WORD}{_NAME_TAIL}{{0,4}}"
    rf"|{_NAME_WORD}{_NAME_TAIL}{{0,4}}[ \t]+"
    r"(?:University|College|Institute|Polytechnic)"
    # "Georgia Institute" / "Massachusetts Institute" stopped here, because the
    # suffix word had to end the name. The trailing "of Technology" is part of
    # both of those schools' actual names.
    rf"(?:[ \t]+of[ \t]+{_NAME_WORD})?"
    r")\b"
)
# Seniority words in a job title, and years-of-experience claims. Without
# these, an experienced resume leaves `seniority` empty and
# app/eligibility.py:candidate_rank falls back to "entry" (rank 1) -- which
# filters the candidate's own level out of discovery entirely.
# The lookahead keeps a student's "senior year"/"senior design project" from
# reading as a job level.
_TITLE_LEVEL_RE = re.compile(
    r"\b(principal|staff|senior|sr|lead|mid[-\s]level|architect)\b"
    r"(?!\s+(?:year|design|project|thesis|capstone|seminar|class))",
    re.I,
)
_LEVEL_WORD_RANK = {
    "mid level": 2, "mid-level": 2,
    "senior": 3, "sr": 3, "lead": 3, "architect": 3,
    "staff": 4, "principal": 4,
}
_RANK_LABEL = {2: "Mid", 3: "Senior", 4: "Staff"}
_YEARS_EXP_RE = re.compile(r"\b(\d{1,2})\+?\s*years?\b", re.I)

_GPA_RE = re.compile(
    r"\b(?:GPA|G\.P\.A\.?)\s*[:\-]?\s*([0-4](?:\.\d{1,2})?)\b", re.I
)
# Resume section headings that PDF extract often glues onto the next line.
_SECTION_NOISE = frozenset({
    "leadership", "experience", "education", "skills", "projects", "awards",
    "activities", "coursework", "summary", "objective", "profile", "work",
    "professional", "relevant", "selected", "technical", "certifications",
    "involvement", "honors", "volunteer", "publications", "research",
    "employment", "campus", "organizations", "affiliations",
})
_GEO_WORDS = frozenset({
    "remote", "hybrid", "onsite", "on-site", "nationwide", "relocate",
    "nyc", "sf", "la", "chicago", "boston", "seattle", "austin", "denver",
    "minneapolis", "atlanta", "dallas", "miami", "portland", "bay", "area",
})
_SKILLISH = frozenset({
    "react", "ai", "ml", "node", "python", "java", "javascript", "typescript",
    "sql", "aws", "docker", "swift", "kotlin", "rust", "go", "golang", "c++",
    "pytorch", "tensorflow", "flask", "django", "fastapi", "mongodb", "redis",
    "graphql", "html", "css", "next.js", "nextjs", "vue", "angular",
})
_YOE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:experience|exp)\b",
    re.I,
)
_GITHUB_USER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9\-]{0,37}[A-Za-z0-9])?$")
_SKILL_WORDS = (
    "python", "javascript", "typescript", "react", "node", "go", "golang",
    "rust", "java", "kotlin", "swift", "c++", "sql", "aws", "gcp", "azure",
    "docker", "kubernetes", "pytorch", "tensorflow", "pandas", "django",
    "flask", "fastapi", "next.js", "graphql", "postgres", "mongodb", "ai",
)


class ProfileImportError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def import_resume(user_id: str, *, text: str = "", filename: str = "",
                  data: bytes | None = None) -> dict:
    raw = (text or "").strip()
    if data:
        raw = _text_from_bytes(filename, data) or raw
    if not raw:
        raise ProfileImportError("Couldn't read any text from that file.")
    extracted = parse_document(raw)
    return apply_extracted(user_id, extracted, source="resume")


def import_github(user_id: str, handle: str) -> dict:
    username = github_username(handle)
    if not username:
        raise ProfileImportError("Need a GitHub username or profile URL.")
    extracted = fetch_github(username)
    return apply_extracted(user_id, extracted, source="github")


def import_linkedin(user_id: str, *, url: str = "", text: str = "",
                    filename: str = "", data: bytes | None = None) -> dict:
    extracted = {"identity": {}, "profile": {}, "knowledge": []}
    warning = ""
    slug_url = linkedin_url(url)
    if slug_url:
        extracted["identity"]["linkedin"] = slug_url
    raw = (text or "").strip()
    if data:
        raw = _text_from_bytes(filename, data) or raw
    if raw:
        parsed = parse_document(raw)
        warning = parsed.get("warning") or ""
        extracted = _merge_extracted(extracted, parsed)
        if slug_url:
            extracted["identity"]["linkedin"] = slug_url
    extracted = _sanitize_extracted(extracted)
    # _merge_extracted and _sanitize_extracted both keep only the three known
    # keys, so the warning has to be reattached rather than carried through.
    if warning:
        extracted["warning"] = warning
    if not extracted["identity"] and not extracted["knowledge"] and not extracted["profile"]:
        raise ProfileImportError(
            "Paste a LinkedIn profile URL, or upload a LinkedIn PDF "
            "(More → Save to PDF on LinkedIn)."
        )
    return apply_extracted(user_id, extracted, source="linkedin")


def apply_extracted(user_id: str, extracted: dict, *, source: str) -> dict:
    """Write empty fields only. Returns what changed plus a coverage snapshot."""
    identity_in = extracted.get("identity") or {}
    profile_in = extracted.get("profile") or {}
    knowledge_in = extracted.get("knowledge") or []

    warning = _clean_text(extracted.get("warning"))
    filled: list[str] = []
    current = applicant.get_identity(user_id)
    to_set: dict = {}
    for key, value in identity_in.items():
        if key not in applicant.FIELDS or key in _EEO_KEYS or key in _BOOL_KEYS:
            continue
        if key in ("full_name", "location"):
            continue
        cleaned = _clean_text(value)
        if not cleaned:
            continue
        if current.get(key) not in (None, ""):
            continue
        to_set[key] = cleaned
        filled.append(_label(key))
    # Education is a list, so it never survives the field loop above -- and it
    # follows the same rule as everything else here: only write what is empty.
    # A single synthesised entry does not count as "already there", but a list
    # the user has actually curated does.
    incoming_school = applicant.clean_education(identity_in.get("education"))
    if len(incoming_school) > 1 and len(applicant.clean_education(current.get("education"))) < 2:
        to_set["education"] = incoming_school
        filled.append("education")
    if to_set:
        applicant.set_identity(user_id, to_set)

    row = profile.get_profile(user_id)
    prof_updates: dict = {}
    for key in _PROFILE_KEYS:
        incoming = _clean_text(profile_in.get(key))
        if not incoming:
            continue
        existing = ""
        if row is not None:
            try:
                existing = (row[key] or "").strip()
            except (IndexError, KeyError):
                existing = ""
        if existing:
            continue
        prof_updates[key] = incoming
        filled.append(_label(key))
    if prof_updates.get("roles") and not prof_updates.get("keywords"):
        if not (row and (row["keywords"] or "").strip()):
            prof_updates["keywords"] = prof_updates["roles"]
    if prof_updates:
        profile.set_profile(user_id, **prof_updates)

    added = 0
    have = {(i["category"], _norm(i["text"])) for i in knowledge.list_all(user_id)}
    for item in knowledge_in:
        if added >= MAX_KNOWLEDGE:
            break
        category = (item.get("category") or "").strip().lower()
        text = _clean_text(item.get("text"))
        if category not in knowledge.CATEGORIES or category == "answer" or not text:
            continue
        if len(text) < 12:
            continue
        key = (category, _norm(text))
        if key in have:
            continue
        if knowledge.add(user_id, category, text, label=item.get("label")):
            have.add(key)
            added += 1
    if added:
        filled.append(f"{added} {'project' if added == 1 else 'projects/facts'}")

    from . import onboarding

    status = onboarding.status(user_id)
    note = _note(source, filled)
    if warning:
        note = f"{note} {warning}".strip() if filled else warning
    return {
        "ok": True,
        "source": source,
        "filled": filled,
        "knowledge_added": added,
        "identity_score": status["identity_score"],
        "identity_missing": status["identity_missing"],
        "has_profile": status["has_profile"],
        "note": note,
        "identity": status["identity"],
        "profile": status["profile"],
        "draft": onboarding.quiz_draft(user_id),
    }


#: Surfaced when the paid overlay did not run. Falling back to the heuristics
#: is by design, but the result is measurably worse, and saying nothing is how
#: a regex-only parse reaches the user looking exactly as confident as a good
#: one -- which is how "React"/"AI" got saved as somebody's city and state.
_SKIP_WARNINGS = {
    "budget": "Today's AI parsing limit is used up, so this was read with the "
              "basic parser. Double-check the fields it filled.",
    "error": "AI parsing wasn't available, so this was read with the basic "
             "parser. Double-check the fields it filled.",
}


def parse_document(text: str) -> dict:
    """Heuristic extract, then Claude overlay when a key is available."""
    heur = _heuristic_parse(text)
    llm, skipped = _llm_parse(text)
    merged = _merge_extracted(heur, llm) if llm else heur
    out = _sanitize_extracted(merged)
    # After the sanitizer, which rebuilds the dict from known keys only.
    if skipped in _SKIP_WARNINGS:
        out["warning"] = _SKIP_WARNINGS[skipped]
    return out


def github_username(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = _GITHUB_RE.search(raw)
    if m:
        login = m.group(1)
        if login.lower() in (
            "orgs", "settings", "explore", "topics", "features", "pulls",
            "issues", "notifications", "new", "login", "signup", "marketplace",
            "sponsors", "about", "pricing", "enterprise", "customer-stories",
        ):
            return ""
        return login
    if _GITHUB_USER_RE.match(raw):
        return raw
    return ""


def linkedin_url(raw: str) -> str:
    m = _LINKEDIN_RE.search(raw or "")
    if not m:
        return ""
    return f"https://www.linkedin.com/in/{m.group(1)}"


def fetch_github(username: str) -> dict:
    identity: dict = {"github": f"https://github.com/{username}"}
    user = _github_get(f"/users/{username}")
    if user is None:
        # Rate limit / outage: still save the URL so Autofill has the link.
        return {
            "identity": identity,
            "profile": {},
            "knowledge": [],
            "warning": (
                "Saved your GitHub URL. Couldn't load the public profile "
                "right now — Autofill still has the link."
            ),
        }
    if user.get("_status") == 404:
        raise ProfileImportError(f"No GitHub user named {username}.")
    name = _clean_text(user.get("name"))
    first, last = _split_name(name)
    if first:
        identity["first_name"] = first
    if last:
        identity["last_name"] = last
    if user.get("email"):
        identity["email"] = str(user["email"]).strip()
    loc = _clean_text(user.get("location"))
    if loc:
        identity.update(_parse_location(loc))
    blog = _clean_text(user.get("blog"))
    if blog:
        if not blog.startswith("http"):
            blog = "https://" + blog
        identity["portfolio"] = blog
    company = _clean_text(user.get("company"))
    if company:
        identity["current_company"] = company.lstrip("@")

    profile_fields: dict = {}
    bio = _clean_text(user.get("bio"))
    if bio:
        profile_fields["resume_summary"] = bio
    if identity.get("city") and identity.get("state"):
        profile_fields["locations"] = f"{identity['city']}, {identity['state']}"

    knowledge_items = []
    langs: list[str] = []
    repos = _github_get(f"/users/{username}/repos?per_page=30&sort=updated")
    if isinstance(repos, list):
        picked = _pick_repos(repos)
        for repo in picked:
            blurb = _repo_blurb(repo)
            if blurb:
                knowledge_items.append({"category": "project", "text": blurb})
            lang = _clean_text(repo.get("language"))
            if lang:
                langs.append(lang.lower())
    if langs:
        profile_fields["keywords"] = ", ".join(dict.fromkeys(langs))

    return {"identity": identity, "profile": profile_fields, "knowledge": knowledge_items}


def _text_from_bytes(filename: str, data: bytes) -> str:
    if not data:
        return ""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ProfileImportError("That file is too large (8 MB max).")
    name = (filename or "").lower()
    if name.endswith(".pdf") or data[:5] == b"%PDF-":
        text = _pdf_text(data)
        if text:
            return text
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="ignore")


#: A column gutter has to be this many characters wide, and this far in from
#: either edge, before we believe it is a gutter and not a wide word gap.
_GUTTER_WIDTH = 4
_GUTTER_EDGE = 12
#: Both sides must carry this many non-empty lines. A sidebar with one line in
#: it is a right-aligned date, not a column.
_COLUMN_LINES = 3


def _split_columns(text: str) -> str | None:
    """Reading order for a two-column page, or None if there is one column.

    Layout extraction preserves where things sit on the page, which is what
    makes a sidebar resume unreadable: the left column's contact details and the
    right column's EDUCATION heading land on the *same line*, so no heading is
    ever at the start of one and every section comes back empty.

    A real gutter is a band of columns that is blank on every single line. That
    is a strict test on purpose -- body text in a one-column resume crosses the
    whole measure, so nothing qualifies and this returns None. Right-aligned
    dates leave a ragged gap, not a clean band, for the same reason.
    """
    lines = [line.rstrip() for line in (text or "").splitlines()]
    if len(lines) < 6:
        return None
    width = max((len(line) for line in lines), default=0)
    if width < 50:
        return None
    occupied = [0] * width
    for line in lines:
        for col, ch in enumerate(line):
            if ch != " ":
                occupied[col] += 1
    runs: list[tuple[int, int]] = []
    col = 0
    while col < width:
        if occupied[col]:
            col += 1
            continue
        start = col
        while col < width and not occupied[col]:
            col += 1
        runs.append((start, col))
    gutters = [
        (s, e) for s, e in runs
        if e - s >= _GUTTER_WIDTH and s >= _GUTTER_EDGE and e <= width - _GUTTER_EDGE
    ]
    # Exactly one: three columns are rare enough that guessing at them would
    # cost more than it saves, and zero means this page is one column.
    if len(gutters) != 1:
        return None
    start, end = gutters[0]
    left = [line[:start].rstrip() for line in lines]
    right = [line[end:].rstrip() for line in lines]
    if min(sum(1 for line in side if line.strip()) for side in (left, right)) < _COLUMN_LINES:
        return None
    return "\n".join(left).strip() + "\n\n" + "\n".join(right).strip()


def _page_text(page) -> str:
    """One page of text, layout mode first.

    pypdf's default extraction emits glyphs in content-stream order, which
    throws away the page's column geometry. A right-aligned date then arrives
    welded to the last word of its line -- "Minneapolis, MNAug 2023" -- and a
    letter-spaced heading arrives split, "EDUCA TION & LEADERSHIP". The second
    is the expensive one: no section regex matches it, so every field under
    that heading gets looked up against the whole document instead of its own
    section, and picks up whatever it finds first.

    Layout mode keeps the columns apart. It is slower and can fail on
    generators pypdf cannot model, so plain extraction stays as the fallback --
    per page, because one odd page should not cost us the rest of the resume.
    """
    for kwargs in ({"extraction_mode": "layout"}, {}):
        try:
            text = page.extract_text(**kwargs) or ""
        except Exception:  # noqa: BLE001 -- try plain, then give this page up
            logger.info("pdf page extract failed (%s)", kwargs or "plain",
                        exc_info=True)
            continue
        if not text.strip():
            continue
        # Only layout text carries the geometry a gutter is detectable in.
        return (_split_columns(text) or text) if kwargs else text
    return ""


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data))
        return "\n".join(_page_text(p) for p in reader.pages[:12]).strip()
    except Exception:  # noqa: BLE001 — fall through to decode
        logger.info("pdf text extract failed", exc_info=True)
        return ""


def _education_block(blob: str) -> str:
    return _section_after(
        blob,
        _heading_re("education", "academic background", "academics"),
        _heading_re("professional experience", "work experience", "work history",
                    "experience", "employment", "key projects", "projects?",
                    "skills", "leadership"),
    )


def _school_from_text(blob: str) -> str:
    if not blob:
        return ""
    found = []
    for match in _SCHOOL_RE.finditer(blob):
        cleaned = _clean_school(match.group(1))
        if cleaned and cleaned not in found:
            found.append(cleaned)
    found.sort(key=lambda s: (s.lower().startswith("university of"), len(s)), reverse=True)
    return found[0] if found else ""


_DEGREE_TOKEN = re.compile(
    r"^(?:B\.?S\.?|B\.?A\.?|M\.?S\.?|M\.?A\.?|M\.?Eng\.?|Ph\.?D\.?|MBA|GPA)$",
    re.I,
)


#: Every continuation word used to require a capital, so the name stopped at the
#: first lowercase connective: "University of Illinois at Urbana-Champaign" came
#: out as "University of Illinois". This ran on the LLM overlay's output too, so
#: even a correct paid extraction got truncated back. Lowercase connectives are
#: now allowed *between* capitalised words; a trailing degree is still trimmed
#: downstream by _DEGREE_TOKEN, which is what keeps "... B.S. Computer Science"
#: out of the school name.
_UNI_NAME_RE = re.compile(rf"(University of {_NAME_WORD}{_NAME_TAIL}{{0,4}})")


def _clean_school(name: str) -> str:
    name = _clean_text(name)
    if not name:
        return ""
    uni = _UNI_NAME_RE.search(name)
    if uni:
        name = uni.group(1)
    words = name.split()
    noise = _SECTION_NOISE | {
        "event", "director", "intern", "software", "engineer", "teaching",
        "assistant", "ambassador", "volunteer", "member", "president",
        "chair", "officer", "lead", "head",
    }
    while words and words[0].strip(".,").lower() in noise:
        words.pop(0)
    cut = next(
        (i for i, word in enumerate(words)
         if _DEGREE_TOKEN.match(word.strip(".,"))),
        None,
    )
    if cut is not None:
        words = words[:cut]
    name = " ".join(words)
    if re.fullmatch(r"(University|College|Institute|Polytechnic|School)", name, re.I):
        return ""
    if not re.search(r"University|College|Institute|Polytechnic|School", name, re.I):
        return ""
    return name


_DEGREE_CANON = (
    ("doctorofphilosophy", "Ph.D."),
    ("bachelorofscience", "B.S."),
    ("bachelorofarts", "B.A."),
    ("masterofengineering", "M.Eng."),
    ("masterofscience", "M.S."),
    ("meng", "M.Eng."),
    ("bsc", "B.S."),
    ("msc", "M.S."),
    ("mba", "MBA"),
    ("phd", "Ph.D."),
    ("bs", "B.S."),
    ("ba", "B.A."),
    ("ms", "M.S."),
)


def _normalize_degree(raw: str) -> str:
    t = _clean_text(raw)
    compact = re.sub(r"[.\s]+", "", t.lower())
    for key, label in _DEGREE_CANON:
        if compact == key or compact.startswith(key):
            return label
    return t


def _degrees_from_text(blob: str) -> list[str]:
    out: list[str] = []
    for match in _DEGREE_RE.finditer(blob or ""):
        deg = _normalize_degree(match.group(1))
        if deg and deg not in out:
            out.append(deg)
        if len(out) >= 3:
            break
    return out


#: The subject named right after a degree token: "B.S. Computer Science".
#: Stops at a bracket, a separator, or the column gap layout extraction leaves,
#: so "B.S. Computer Science(May 2026)   Undergrad GPA" yields the subject only.
_DISCIPLINE_AFTER_DEGREE = re.compile(
    r"^[\s,.:]*(?:in|of)?[ \t]*([A-Z][A-Za-z]*(?:[ \t][A-Z][A-Za-z]*){0,3})"
)
_IN_PROGRESS_HINT = re.compile(
    r"\b(in progress|ongoing|present|current|expected|pursuing|candidate)\b", re.I
)


def _education_entries(block: str, school: str) -> list[dict]:
    """One entry per degree named in the education section.

    Both degrees frequently share a line -- "M.S. Data Science (In Progress)
    B.S. Computer Science (May 2026)" is an ordinary way to write it -- so this
    splits on the degree tokens themselves rather than on newlines, and reads
    each segment up to the next degree.

    The school is carried across every entry. Two degrees from two different
    institutions in one block is a harder problem than it looks, and guessing
    at it would put the wrong university next to a degree; the list can be
    corrected in the app, and a single wrong school is worse than one repeated.
    """
    if not block:
        return []
    marks = [m for m in _DEGREE_RE.finditer(block)]
    if len(marks) < 2:
        return []
    entries: list[dict] = []
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(block)
        tail = block[mark.end():end]
        entry: dict[str, str] = {"degree": _normalize_degree(mark.group(1))}
        if school:
            entry["school"] = school
        # _DEGREE_RE can swallow the subject itself ("B.S. Computer Science"),
        # so the known-vocabulary matcher reads the whole segment first and the
        # after-the-token pattern only covers what that vocabulary misses.
        segment = block[mark.start():end]
        disc = _discipline_from_text(segment)
        if not disc:
            loose = _DISCIPLINE_AFTER_DEGREE.match(tail)
            disc = _clean_text(loose.group(1)) if loose else ""
        if disc:
            entry["discipline"] = disc
        year = _GRAD_YEAR_RE.search(tail)
        if year:
            entry["grad_year"] = year.group(1)
        gpa = _GPA_RE.search(tail)
        if gpa:
            entry["gpa"] = gpa.group(1)
        if _IN_PROGRESS_HINT.search(tail):
            entry["status"] = "in_progress"
        elif entry.get("grad_year"):
            entry["status"] = "completed"
        if entry.get("degree"):
            entries.append(entry)
    return entries


def _discipline_from_text(blob: str) -> str:
    if re.search(r"data science", blob, re.I):
        return "Data Science"
    if re.search(r"computer science|\bCS\b", blob, re.I):
        return "Computer Science"
    if re.search(r"software engineering", blob, re.I):
        return "Software Engineering"
    if re.search(r"electrical engineering|\bEE\b", blob, re.I):
        return "Electrical Engineering"
    return ""


def _candidate_years(text: str) -> list[str]:
    """Years in ``text``, minus any that only ever open an unfinished range."""
    if not text:
        return []
    skip = [m.span(1) for m in _ONGOING_RE.finditer(text)]
    return [
        m.group(1) for m in _GRAD_YEAR_RE.finditer(text)
        if not any(lo <= m.start() < hi for lo, hi in skip)
    ]


def _grad_year_from_text(edu: str, blob: str) -> str:
    """When the degree *ends* -- the latest education year, not the first one.

    An explicit promise ("expected May 2026", "Class of 2027") wins outright.
    Failing that the answer is the largest year in the education section,
    because graduation is the last thing that happens in one.

    This used to take the first month-labelled year it found, which on the
    commonest header there is -- an enrolment range on the school line and the
    graduation date beside the degree --

        University of Minnesota, Minneapolis, MN    Aug 2023 - Present
        B.S. Computer Science(May 2026)

    returned 2023, the year he *started*. That is not a cosmetic error: it went
    onto applications as a graduation year, and _roles_and_seniority reads the
    same field to decide whether someone is a new grad, so it also mis-sorted
    every job he was shown.

    A year that only ever opens an unfinished range is dropped rather than
    guessed from: "Aug 2023 - Present" with no end date means we do not know
    when they graduate, and an empty field the user fills in beats a wrong one
    autofilled onto a real application.
    """
    search = edu or blob
    for pattern in _GRAD_LABELLED:
        hits = re.findall(pattern, search, re.I)
        if hits:
            return max(hits)
    years = _candidate_years(search)
    if years:
        return max(years)
    # No second sweep of the whole document. When there *was* an education
    # section and it deliberately yielded nothing -- an unfinished degree with
    # no end date -- that silence is the answer, and scavenging the rest of the
    # page hands back the year off somebody's summer internship instead.
    return ""


def _roles_and_seniority(blob: str, grad_year: str) -> dict:
    head = blob[:3000]
    has_intern = bool(re.search(r"\bintern(?:ship)?\b", head, re.I))
    has_newgrad = bool(re.search(
        r"new\s+grad|recent\s+grad|class of 202[5-9]|"
        r"expected\s+20(?:2[6-9]|3\d)",
        blob,
        re.I,
    ))
    if grad_year and grad_year >= "2025":
        has_newgrad = True
    has_swe = bool(re.search(
        r"software engineer|full[ -]?stack|backend|front[ -]?end|\bswe\b|"
        r"developer",
        head,
        re.I,
    ))
    has_ml = bool(re.search(
        r"machine learning|data scien|\bml engineer", head, re.I
    ))
    roles: list[str] = []
    if has_swe:
        roles.append("software engineer")
        if has_intern:
            roles.append("software intern")
    if has_ml:
        roles.append("machine learning engineer")
        roles.append("data scientist")
        if has_intern:
            roles.append("ML intern")
    if not roles and has_intern:
        roles.append("intern")
    if not roles and has_newgrad:
        roles.append("new grad SWE")
    seniority: list[str] = []
    if has_intern:
        seniority.append("Internship")
    if has_newgrad:
        seniority.append("New grad")
    if not has_intern and not has_newgrad:
        # Highest level named in a title wins; else infer from years claimed.
        ranks = [
            _LEVEL_WORD_RANK[w]
            for w in (m.group(1).lower()
                      for m in _TITLE_LEVEL_RE.finditer(head))
            if w in _LEVEL_WORD_RANK
        ]
        rank = max(ranks) if ranks else None
        if rank is None:
            years = max((int(y) for y in _YEARS_EXP_RE.findall(head)), default=0)
            if years >= 6:
                rank = 3
            elif years >= 3:
                rank = 2
        if rank in _RANK_LABEL:
            seniority.append(_RANK_LABEL[rank])
    out: dict = {}
    if roles:
        out["roles"] = ", ".join(dict.fromkeys(roles))
    if seniority:
        out["seniority"] = ", ".join(dict.fromkeys(seniority))
    return out


def _is_geo_location(text: str) -> bool:
    parts = [p.strip() for p in re.split(r"[,;/|]+", text or "") if p.strip()]
    if not parts:
        return False
    skillish = 0
    geo = 0
    for part in parts:
        tok = part.lower().strip()
        words = set(re.findall(r"[a-z0-9.+#]+", tok))
        if words & _SKILLISH or tok in _SKILLISH:
            skillish += 1
            continue
        if _find_city_state(part) or words & _GEO_WORDS:
            geo += 1
            continue
        if re.search(r"\b(city|area|county|metro)\b", tok):
            geo += 1
            continue
        if tok in {s.lower() for s in _SKILL_WORDS}:
            skillish += 1
        elif len(tok) == 2 and tok.isalpha():
            geo += 1
        else:
            geo += 1
    return geo >= skillish and skillish < max(1, len(parts))


def _sanitize_extracted(data: dict) -> dict:
    ident = dict(data.get("identity") or {})
    prof = dict(data.get("profile") or {})
    school = _clean_school(ident.get("school") or "")
    if school:
        ident["school"] = school
    else:
        ident.pop("school", None)
    if ident.get("linkedin"):
        url = linkedin_url(str(ident["linkedin"]))
        if url:
            ident["linkedin"] = url
    if ident.get("github"):
        user = github_username(str(ident["github"]))
        if user:
            ident["github"] = f"https://github.com/{user}"
    locs = _clean_text(prof.get("locations"))
    if locs and not _is_geo_location(locs):
        prof.pop("locations", None)
        if ident.get("city") and ident.get("state"):
            city_state = f"{ident['city']}, {ident['state']}"
            if _is_geo_location(city_state):
                prof["locations"] = city_state
    elif locs:
        prof["locations"] = locs
    roles = _clean_text(prof.get("roles"))
    if re.fullmatch(r"interns?(hip)?", roles, re.I):
        kw = (prof.get("keywords") or "").lower()
        if any(s in kw for s in ("python", "java", "react", "typescript", "javascript")):
            prof["roles"] = "software engineer intern"
    return {
        "identity": ident,
        "profile": prof,
        "knowledge": list(data.get("knowledge") or []),
        **({"warning": data["warning"]} if data.get("warning") else {}),
    }


def _heuristic_parse(text: str) -> dict:
    blob = text[: MAX_PARSE_CHARS * 2]
    identity: dict = {}
    emails = _EMAIL_RE.findall(blob)
    if emails:
        identity["email"] = emails[0]
    phones = _PHONE_RE.findall(blob)
    if phones:
        identity["phone"] = phones[0]
    li = _LINKEDIN_RE.search(blob)
    if li:
        identity["linkedin"] = f"https://www.linkedin.com/in/{li.group(1)}"
    gh = _GITHUB_RE.search(blob)
    if gh:
        identity["github"] = f"https://github.com/{gh.group(1)}"

    loc = _find_city_state(blob[:2000])
    if loc:
        identity["city"], identity["state"] = loc

    first, last = _name_from_header(blob)
    if first:
        identity["first_name"] = first
    if last:
        identity["last_name"] = last

    edu = _education_block(blob)
    school = _school_from_text(edu) or _school_from_text(blob)
    if school:
        identity["school"] = school
    degrees = _degrees_from_text(edu or blob)
    if degrees:
        identity["degree"] = ", ".join(degrees[:2])
    disc = _discipline_from_text(edu or blob)
    if disc:
        identity["discipline"] = disc
    gpa = _GPA_RE.search(edu or blob)
    if gpa:
        identity["gpa"] = gpa.group(1)
    gy = _grad_year_from_text(edu, blob)
    if gy:
        identity["grad_year"] = gy
    # Only when there is genuinely more than one. A single degree is exactly
    # what the flat fields already say, and applicant.get_identity presents it
    # as a one-entry list anyway -- so emitting one here would add a stored
    # structure that changes nothing and could only introduce a difference.
    schooling = _education_entries(edu, identity.get("school", ""))
    if len(schooling) > 1:
        identity["education"] = schooling
    # Work. None of this was ever read: the experience section was mined for
    # free-text bullets and nothing else, so "current company", "current title"
    # and "years of experience" came back empty from a resume that named all
    # three, and the quiz had nothing to prefill them with.
    jobs = sorted(_work_history(blob), key=lambda j: j["end"], reverse=True)
    if jobs:
        latest = jobs[0]
        if latest["company"]:
            identity["current_company"] = latest["company"]
        if latest["title"]:
            identity["current_title"] = latest["title"]
    yoe = _YOE_RE.search(blob)
    if yoe:
        # A resume that states the number outright is answering the question
        # forms ask; believe it over anything counted off the dates.
        identity["years_experience"] = yoe.group(1)
    elif jobs:
        # Floor, not round. This lands on real applications, and a person with
        # sixteen months has one year of experience, not two. Understating is
        # correctable in the next field; overstating is a false claim.
        identity["years_experience"] = str(_months_worked(jobs) // 12)

    profile_fields: dict = {}
    skills = [s for s in _SKILL_WORDS if re.search(rf"\b{re.escape(s)}\b", blob, re.I)]
    if skills:
        profile_fields["keywords"] = ", ".join(dict.fromkeys(skills))
    profile_fields.update(_roles_and_seniority(blob, identity.get("grad_year") or ""))

    locs: list[str] = []
    if identity.get("city") and identity.get("state"):
        locs.append(f"{identity['city']}, {identity['state']}")
    if re.search(r"\bremote\b", blob[:2500], re.I):
        locs.append("Remote")
    if locs:
        profile_fields["locations"] = ", ".join(dict.fromkeys(locs))

    knowledge_items = _experience_from_text(blob) + _projects_from_text(blob)
    return _sanitize_extracted(
        {"identity": identity, "profile": profile_fields, "knowledge": knowledge_items}
    )


_EXTRACT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["identity", "profile", "knowledge"],
    "properties": {
        "identity": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                k: {"type": "string"} for k in (
                    "first_name", "last_name", "email", "phone", "address",
                    "city", "state", "zip", "country", "linkedin", "github",
                    "portfolio", "school", "degree", "discipline", "gpa",
                    "grad_year", "current_company", "current_title",
                    "years_experience", "start_date", "work_arrangement",
                )
            },
        },
        "profile": {
            "type": "object",
            "additionalProperties": False,
            "properties": {k: {"type": "string"} for k in _PROFILE_KEYS},
        },
        "knowledge": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["category", "text"],
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["experience", "project", "achievement", "strength", "preference"],
                    },
                    "text": {"type": "string"},
                },
            },
        },
    },
}


def _llm_parse(text: str) -> tuple[dict | None, str]:
    s = get_settings()
    if not s.use_llm_router:
        # No key configured: heuristics are the whole design here, not a
        # degraded mode, so there is nothing to warn anyone about.
        return None, ""
    from . import llm_budget

    if not llm_budget.consume(feature="parse"):
        logger.info("resume llm parse skipped: daily parse slice spent (cap=%s)",
                    llm_budget.feature_cap("parse"))
        return None, "budget"
    try:
        from . import llm_health
        client = llm_health.client(s.anthropic_api_key)
        snippet = text[:MAX_PARSE_CHARS]
        resp = client.messages.create(
            model=s.anthropic_model,
            max_tokens=1500,
            system=(
                "Extract facts from this resume or LinkedIn PDF for a job-application "
                "profile. Use ONLY text that is present — never invent employers, "
                "dates, GPAs, or projects. Unknown fields are empty strings. "
                "Do not extract gender, race, veteran, disability, or other "
                "demographic fields. Knowledge items are short first-person facts. "
                "Split work vs projects: category 'experience' is internships, jobs, "
                "TA, or ambassador roles and MUST include the employer city/state "
                "(e.g. Chicago, IL). Category 'project' is personal or GitHub work "
                "and must NOT include an employer location. "
                "locations are geographic only (cities, regions, Remote) — never "
                "skills, libraries, or 'AI'. school is the institution name only "
                "(e.g. University of Minnesota), never a section header like "
                "LEADERSHIP or EDUCATION. grad_year is expected or most recent "
                "graduation, not an internship year. roles are job titles they want "
                "(software engineer, software intern) — never a lone word 'intern' "
                "if they study CS. If they have internships and a 2025+ graduation, "
                "seniority is 'Internship, New grad'. "
                "current_company and current_title are the most recent job in the "
                "experience section -- internships and research roles count, and "
                "'most recent' means latest end date, not first listed. "
                "years_experience is a whole number of years of professional work, "
                "counted from the employment dates when the resume does not state "
                "it outright; round down, and use '0' rather than leaving it empty "
                "when the only work is a short internship."
            ),
            messages=[{"role": "user", "content": snippet}],
            output_config={"format": {"type": "json_schema", "schema": _EXTRACT_SCHEMA}},
        )
        payload = next((b.text for b in resp.content if b.type == "text"), "")
        data = json.loads(payload)
        if not isinstance(data, dict):
            return None, "error"
        return {
            "identity": {k: v for k, v in (data.get("identity") or {}).items() if v},
            "profile": {k: v for k, v in (data.get("profile") or {}).items() if v},
            "knowledge": [
                i for i in (data.get("knowledge") or [])
                if isinstance(i, dict) and i.get("text")
            ],
        }, ""
    except Exception:  # noqa: BLE001 — fail open to heuristics
        logger.info("resume llm parse failed; using heuristics", exc_info=True)
        return None, "error"


def _github_get(path: str):
    """JSON from api.github.com, or ``{\"_status\": 404}``, or None on network failure."""
    import httpx

    s = get_settings()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "JobPilot/1.0",
    }
    token = (s.github_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = "https://api.github.com" + path
    try:
        resp = httpx.get(url, headers=headers, timeout=8.0, follow_redirects=True)
    except Exception:  # noqa: BLE001
        logger.info("github fetch failed: %s", path, exc_info=True)
        return None
    if resp.status_code == 404:
        return {"_status": 404}
    if resp.status_code >= 400:
        logger.info("github HTTP %s for %s", resp.status_code, path)
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _pick_repos(repos: list) -> list:
    usable = [
        r for r in repos
        if isinstance(r, dict)
        and not r.get("fork")
        and not r.get("archived")
        and (r.get("description") or r.get("name"))
    ]
    usable.sort(key=lambda r: int(r.get("stargazers_count") or 0), reverse=True)
    return usable[:5]


def _repo_blurb(repo: dict) -> str:
    name = _clean_text(repo.get("name"))
    desc = _clean_text(repo.get("description"))
    url = _clean_text(repo.get("html_url"))
    lang = _clean_text(repo.get("language"))
    if not name:
        return ""
    bits = [name]
    if desc:
        bits.append(desc.rstrip("."))
    extra = []
    if lang:
        extra.append(lang)
    if url:
        extra.append(url)
    text = " — ".join(bits)
    if extra:
        text += " (" + ", ".join(extra) + ")"
    return text


#: One trailing word of a compound heading. The capital is load-bearing and
#: case-sensitive on purpose: real headings are ALL CAPS or Title Case, so
#: requiring one is what stops a line of prose that merely opens with the
#: keyword ("Education outreach for local schools") from being read as the
#: EDUCATION heading. ``_section_after`` searches with ``re.I``, which would
#: otherwise make ``[A-Z]`` match anything at all, hence the scoped ``(?-i:)``.
_HEAD_WORD = r"(?-i:[A-Z])[\w'&/-]*"
#: What may join a heading to its tail: punctuation, a connective, or a space.
_HEAD_JOIN = r"(?:[ \t]*[&/+,][ \t]*|[ \t]+(?:and|of)[ \t]+|[ \t]+)"


def _heading_re(*words: str) -> str:
    """A regex matching a section heading line.

    Headings are rarely the bare keyword. Resumes write "EDUCATION &
    LEADERSHIP", "Skills and Awards", "EDUCATION:" -- and the patterns here
    used to demand the keyword followed by nothing but a newline, so a compound
    heading matched *nothing* and the section came back empty. Up to three
    capitalised tail words are allowed: enough for the real compounds, not
    enough to swallow a paragraph.

    Pass the longer alternatives first ("professional experience" before
    "experience"); alternation takes the first branch that matches.
    """
    return (
        rf"(?:^|\n)[ \t]*(?:{'|'.join(words)})"
        rf"(?:{_HEAD_JOIN}{_HEAD_WORD}){{0,3}}"
        r"[ \t]*:?[ \t]*\n"
    )


def _section_after(blob: str, heading: str, stop: str) -> str:
    m = re.search(heading, blob, re.I)
    if not m:
        return ""
    rest = blob[m.end():]
    nxt = re.search(stop, rest, re.I)
    return rest[: nxt.start() if nxt else 1500]


#: A job header is recognised by its dates. Everything else on the line varies
#: wildly between resumes; a month/year range does not.
_MONTH_WORD = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
_MONTH_INDEX = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_DATE_POINT = rf"(?:({_MONTH_WORD})[.,]?\s*)?((?:19|20)\d{{2}})"
_STILL_THERE = r"(present|current|now|ongoing|today)"
_DATE_RANGE_RE = re.compile(
    rf"{_DATE_POINT}\s*(?:[-–—]{{1,2}}|\bto\b)\s*(?:{_STILL_THERE}|{_DATE_POINT})",
    re.I,
)
#: Words that make a phrase a job title rather than an employer.
_TITLE_WORDS = (
    "engineer", "developer", "programmer", "scientist", "analyst", "manager",
    "intern", "researcher", "designer", "consultant", "associate", "assistant",
    "architect", "administrator", "specialist", "technician", "lead",
    "director", "founder", "officer", "coordinator", "instructor", "tutor",
    "ambassador", "fellow", "trainee", "apprentice", "strategist", "recruiter",
    "accountant", "auditor", "paralegal", "nurse", "technologist",
)
_TITLE_WORD_RE = re.compile(r"\b(?:%s)s?\b" % "|".join(_TITLE_WORDS), re.I)
#: Splits a header into its parts. Resumes use a dash, a pipe, a bullet, "at",
#: or a plain comma between employer and title, and no two agree on the order.
_HEADER_SPLIT_RE = re.compile(r"\s*(?:[|•·]|[-–—]{1,2}|\bat\b|,)\s*", re.I)


def _month_number(word: str) -> int:
    return _MONTH_INDEX.get((word or "")[:3].lower(), 0)


def _range_months(m: re.Match) -> tuple[tuple[int, int], tuple[int, int], bool]:
    """((start_year, start_month), (end_year, end_month), still_there)."""
    start = (int(m.group(2)), _month_number(m.group(1)) or 1)
    if m.group(3):
        now = datetime.now(timezone.utc)
        return start, (now.year, now.month), True
    end_year = int(m.group(5)) if m.group(5) else start[0]
    end_month = _month_number(m.group(4)) or 12
    return start, (end_year, end_month), False


def _looks_like_a_place(part: str) -> bool:
    if _CITY_STATE_RE.search(part):
        return True
    words = [w.strip(".,").lower() for w in part.split()]
    return bool(words) and all(w in _GEO_WORDS or len(w) <= 2 for w in words)


def _header_parts(text: str) -> list[str]:
    out = []
    for part in _HEADER_SPLIT_RE.split(text):
        part = _clean_text(part).strip(" .,;:|-–—")
        # A bare state code or a stray year is debris from the split, not a name.
        if len(part) < 2 or part.isdigit():
            continue
        out.append(part)
    return out


def _work_history(blob: str) -> list[dict]:
    """Structured jobs from the experience section: employer, title, dates.

    ``_experience_from_text`` already reads this section, but only as free-text
    bullets for the knowledge base — nothing was ever pulled out as *fields*,
    which is why "current company", "current title" and "years of experience"
    stayed empty after an import even though the resume named all three.

    Entries are found by their date range rather than their shape. Where the
    employer and the title sit relative to each other, which separator joins
    them, and whether the dates share their line is different on every resume;
    a month-and-year range is the one thing that is reliably present and
    reliably means "a job starts here".
    """
    section = _section_after(
        blob,
        _heading_re("professional experience", "work experience", "work history",
                    "experience", "employment"),
        _heading_re("key projects", "selected projects", "projects?",
                    "education", "skills", "awards", "publications"),
    )
    if not section:
        return []
    lines = [_clean_text(line) for line in section.splitlines()]
    jobs: list[dict] = []
    for i, line in enumerate(lines):
        if not line:
            continue
        match = _DATE_RANGE_RE.search(line)
        if not match:
            continue
        start, end, still_there = _range_months(match)
        # The header is whatever shares the date's line, unless that is only a
        # location (a very common two-line layout) -- then it is the line above.
        # Strip "San Francisco, CA" before splitting, not after: the split
        # treats a comma as a separator, which would tear a city off its state
        # and leave "San Francisco" looking exactly like an employer name.
        remainder = _CITY_STATE_RE.sub(" ", _DATE_RANGE_RE.sub(" ", line))
        parts = _header_parts(remainder)
        named = [p for p in parts if not _looks_like_a_place(p)]
        if not named:
            previous = next((lines[j] for j in range(i - 1, -1, -1) if lines[j]), "")
            if previous and len(previous) <= 120:
                named = [p for p in _header_parts(previous) if not _looks_like_a_place(p)]
        if not named:
            continue
        title = next((p for p in named if _TITLE_WORD_RE.search(p)), "")
        company = next((p for p in named if p != title), "")
        if not company and not title:
            continue
        # A range with no month is weak evidence on its own: prose carries bare
        # year spans too. "Analyzed 2019 - 2021 revenue trends across regions"
        # was being read as a job at a company called "Analyzed revenue trends
        # across regions", and its two invented years went into the experience
        # total. Require a recognisable job title before believing one.
        if not (match.group(1) or match.group(4)) and not title:
            continue
        jobs.append({
            "company": company, "title": title,
            "start": start, "end": end, "still_there": still_there,
        })
        if len(jobs) >= 12:
            break
    return jobs


def _months_worked(jobs: list[dict]) -> int:
    """Distinct calendar months across every job.

    A set rather than a sum: two overlapping roles (a part-time job held
    through an internship, or a promotion written as two entries) are one
    stretch of experience, and adding their lengths would report twice what
    the person actually worked.
    """
    covered: set[tuple[int, int]] = set()
    for job in jobs:
        (sy, sm), (ey, em) = job["start"], job["end"]
        if (ey, em) < (sy, sm):
            continue
        year, month = sy, sm
        while (year, month) <= (ey, em) and len(covered) < 900:
            covered.add((year, month))
            month += 1
            if month > 12:
                year, month = year + 1, 1
    return len(covered)


def _experience_from_text(blob: str) -> list[dict]:
    """Grab bullets under an EXPERIENCE heading. Jobs keep their location line."""
    section = _section_after(
        blob,
        _heading_re("professional experience", "work experience", "experience",
                    "employment"),
        _heading_re("key projects", "selected projects", "projects?",
                    "education", "skills"),
    )
    if not section:
        return []
    items = []
    for line in section.splitlines():
        # _clean_text, not .strip(): layout extraction separates columns with
        # runs of spaces, so a bullet arrives as "Shipped 20+ tickets<gap>2025".
        line = _clean_text(re.sub(r"^[\s\-\*•·]+", "", line))
        if len(line) < 16 or len(line) > 320:
            continue
        if line.isupper():
            continue
        items.append({"category": "experience", "text": line, "label": None})
        if len(items) >= 6:
            break
    return items


def _projects_from_text(blob: str) -> list[dict]:
    """Grab bullets under a PROJECTS heading when there is no LLM pass."""
    section = _section_after(
        blob,
        _heading_re("key projects", "selected projects", "personal projects",
                    "projects?"),
        _heading_re("professional experience", "work experience", "work history",
                    "experience", "education", "skills", "awards"),
    )
    if not section:
        return []
    items = []
    for line in section.splitlines():
        # _clean_text, not .strip(): layout extraction separates columns with
        # runs of spaces, so a bullet arrives as "Shipped 20+ tickets<gap>2025".
        line = _clean_text(re.sub(r"^[\s\-\*•·]+", "", line))
        if len(line) < 16 or len(line) > 280:
            continue
        if line.isupper():
            continue
        items.append({"category": "project", "text": line})
        if len(items) >= 4:
            break
    return items


def _name_from_header(blob: str) -> tuple[str, str]:
    for line in blob.splitlines()[:10]:
        line = line.strip()
        if not line or "@" in line or "http" in line.lower():
            continue
        if _PHONE_RE.search(line):
            continue
        words = line.split()
        if 2 <= len(words) <= 4 and all(w[0].isalpha() for w in words if w):
            if sum(w[0].isupper() for w in words) >= 2:
                return _split_name(line)
    return "", ""


def _split_name(full: str) -> tuple[str, str]:
    parts = [p for p in (full or "").split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _parse_location(raw: str) -> dict:
    m = _find_city_state(raw)
    if m:
        return {"city": m[0], "state": m[1]}
    if "," in raw:
        city, rest = raw.split(",", 1)
        city, rest = city.strip(), rest.strip()
        out = {}
        if city:
            out["city"] = city
        # Same whitelist as the regex path. Without it this fallback re-opens
        # the hole the regex just closed: "React, AI" splits into a two-letter
        # alphabetic remainder and becomes a state again.
        if len(rest) == 2 and rest.upper() in _US_STATES:
            out["state"] = rest.upper()
        elif rest:
            out["country"] = rest
        return out
    if raw:
        return {"city": raw}
    return {}


def _merge_extracted(base: dict, overlay: dict) -> dict:
    ident = dict(base.get("identity") or {})
    for k, v in (overlay.get("identity") or {}).items():
        if _clean_text(v):
            ident[k] = v
    prof = dict(base.get("profile") or {})
    for k, v in (overlay.get("profile") or {}).items():
        if _clean_text(v):
            prof[k] = v
    know = list(base.get("knowledge") or [])
    seen = {_norm(i.get("text", "")) for i in know}
    for item in overlay.get("knowledge") or []:
        text = _clean_text(item.get("text") if isinstance(item, dict) else "")
        if not text or _norm(text) in seen:
            continue
        know.append(item)
        seen.add(_norm(text))
    return {"identity": ident, "profile": prof, "knowledge": know}


def _clean_text(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _label(key: str) -> str:
    return key.replace("_", " ")


def _note(source: str, filled: list[str]) -> str:
    if source == "linkedin" and filled and all(
        "linkedin" in f.lower() for f in filled
    ):
        return (
            "Saved your LinkedIn URL for Autofill. LinkedIn doesn't let apps "
            "read the profile page — upload a LinkedIn PDF (More → Save to PDF) "
            "to fill school, jobs, and skills."
        )
    if not filled:
        return "Nothing new to add — those details were already on your profile."
    n = len(filled)
    if source == "github":
        verb = "Pulled from GitHub"
    elif source == "linkedin":
        verb = "Saved from LinkedIn"
    else:
        verb = "Read from your resume"
    preview = ", ".join(filled[:6])
    extra = f" (+{n - 6} more)" if n > 6 else ""
    return f"{verb}: {preview}{extra}."
