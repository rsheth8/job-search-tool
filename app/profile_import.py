"""Fill a profile from a resume, a GitHub user, or a LinkedIn URL.

Imports only write *empty* fields so a quiz skip or a later edit is never
overwritten. LinkedIn pages are not scraped (blocked + against their terms);
a profile URL is stored, and a LinkedIn PDF goes through the resume parser.
"""
from __future__ import annotations

import json
import logging
import re
from io import BytesIO

from . import applicant, knowledge, profile
from .config import get_settings

logger = logging.getLogger("profile_import")

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_PARSE_CHARS = 24_000
MAX_KNOWLEDGE = 6

_EEO_KEYS = frozenset({
    "gender", "race", "ethnicity", "veteran_status", "disability_status",
})
_BOOL_KEYS = frozenset(applicant.BOOL_FIELDS)

_PROFILE_KEYS = ("roles", "keywords", "locations", "seniority", "resume_summary")

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b", re.I)
_PHONE_RE = re.compile(
    r"(?:\+?1[\s.\-]?)?(?:\(?\d{3}\)?[\s.\-]?)\d{3}[\s.\-]?\d{4}\b"
)
_LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:www\.)?linkedin\.com/in/([A-Za-z0-9\-_%]+)/?", re.I
)
_GITHUB_RE = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9](?:[A-Za-z0-9\-]{0,37}[A-Za-z0-9])?)/?",
    re.I,
)
_CITY_STATE_RE = re.compile(
    r"\b([A-Z][a-z]+(?:[\s\-][A-Z][a-z]+)*),\s*([A-Z]{2})\b"
)
_GRAD_YEAR_RE = re.compile(r"\b(20[1-3]\d)\b")
_DEGREE_RE = re.compile(
    r"\b((?:B\.?S\.?|B\.?A\.?|M\.?S\.?|M\.?Eng\.?|Ph\.?D\.?|MBA|"
    r"Bachelor(?:'s)?(?: of [^,\n]+)?|Master(?:'s)?(?: of [^,\n]+)?)"
    r"(?:\s+(?:of\s+)?(?:Science|Arts|Engineering|Computer Science))?)",
    re.I,
)
_SCHOOL_RE = re.compile(
    r"\b((?:[A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,5}\s+)?"
    r"(?:University|College|Institute|Polytechnic)"
    r"(?:\s+of\s+[A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){0,4})?)\b"
)
_YOE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:experience|exp)\b",
    re.I,
)
_GITHUB_USER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9\-]{0,37}[A-Za-z0-9])?$")
_SKILL_WORDS = (
    "python", "javascript", "typescript", "react", "node", "go", "golang",
    "rust", "java", "kotlin", "swift", "c++", "sql", "aws", "gcp", "azure",
    "docker", "kubernetes", "pytorch", "tensorflow", "pandas", "django",
    "flask", "fastapi", "next.js", "graphql", "postgres", "mongodb",
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
    slug_url = linkedin_url(url)
    if slug_url:
        extracted["identity"]["linkedin"] = slug_url
    raw = (text or "").strip()
    if data:
        raw = _text_from_bytes(filename, data) or raw
    if raw:
        parsed = parse_document(raw)
        extracted = _merge_extracted(extracted, parsed)
        if slug_url:
            extracted["identity"]["linkedin"] = slug_url
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
    return {
        "ok": True,
        "source": source,
        "filled": filled,
        "knowledge_added": added,
        "identity_score": status["identity_score"],
        "identity_missing": status["identity_missing"],
        "has_profile": status["has_profile"],
        "note": _note(source, filled),
        "identity": status["identity"],
        "profile": status["profile"],
    }


def parse_document(text: str) -> dict:
    """Heuristic extract, then Claude overlay when a key is available."""
    heur = _heuristic_parse(text)
    llm = _llm_parse(text)
    if llm:
        return _merge_extracted(heur, llm)
    return heur


def github_username(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    m = _GITHUB_RE.search(raw)
    if m:
        login = m.group(1)
        if login.lower() in ("orgs", "settings", "explore", "topics", "features"):
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
    user = _github_get(f"/users/{username}")
    if user is None:
        raise ProfileImportError("Couldn't reach GitHub. Try again in a moment.", 502)
    if user.get("_status") == 404:
        raise ProfileImportError(f"No GitHub user named {username}.")
    identity: dict = {"github": f"https://github.com/{username}"}
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

    knowledge_items = []
    repos = _github_get(f"/users/{username}/repos?per_page=30&sort=updated")
    if isinstance(repos, list):
        picked = _pick_repos(repos)
        for repo in picked:
            blurb = _repo_blurb(repo)
            if blurb:
                knowledge_items.append({"category": "project", "text": blurb})

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


def _pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data))
        parts = []
        for page in reader.pages[:12]:
            parts.append(page.extract_text() or "")
        return "\n".join(parts).strip()
    except Exception:  # noqa: BLE001 — fall through to decode
        logger.info("pdf text extract failed", exc_info=True)
        return ""


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

    loc = _CITY_STATE_RE.search(blob[:2000])
    if loc:
        identity["city"] = loc.group(1)
        identity["state"] = loc.group(2)

    first, last = _name_from_header(blob)
    if first:
        identity["first_name"] = first
    if last:
        identity["last_name"] = last

    school = _SCHOOL_RE.search(blob)
    if school:
        identity["school"] = _clean_text(school.group(1))
    degree = _DEGREE_RE.search(blob)
    if degree:
        identity["degree"] = _clean_text(degree.group(1))
    if re.search(r"computer science|\bCS\b", blob, re.I):
        identity.setdefault("discipline", "Computer Science")
    gy = _GRAD_YEAR_RE.search(blob)
    if gy:
        identity["grad_year"] = gy.group(1)
    yoe = _YOE_RE.search(blob)
    if yoe:
        identity["years_experience"] = yoe.group(1)

    profile_fields: dict = {}
    skills = [s for s in _SKILL_WORDS if re.search(rf"\b{re.escape(s)}\b", blob, re.I)]
    if skills:
        profile_fields["keywords"] = ", ".join(dict.fromkeys(skills) )
    if re.search(r"\bintern(?:ship)?\b", blob[:1500], re.I):
        profile_fields["seniority"] = "Internship"
        profile_fields.setdefault("roles", "intern")
    elif re.search(r"new\s+grad|recent\s+grad", blob[:1500], re.I):
        profile_fields["seniority"] = "New grad"
        profile_fields.setdefault("roles", "new grad SWE")
    elif re.search(r"software engineer|developer|swe\b", blob[:2000], re.I):
        profile_fields.setdefault("roles", "software engineer")

    if identity.get("city") and identity.get("state"):
        profile_fields.setdefault(
            "locations", f"{identity['city']}, {identity['state']}"
        )

    knowledge_items = _experience_from_text(blob) + _projects_from_text(blob)
    return {"identity": identity, "profile": profile_fields, "knowledge": knowledge_items}


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


def _llm_parse(text: str) -> dict | None:
    s = get_settings()
    if not s.use_llm_router:
        return None
    from . import llm_budget

    if not llm_budget.consume():
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=s.anthropic_api_key)
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
                "and must NOT include an employer location."
            ),
            messages=[{"role": "user", "content": snippet}],
            output_config={"format": {"type": "json_schema", "schema": _EXTRACT_SCHEMA}},
        )
        payload = next((b.text for b in resp.content if b.type == "text"), "")
        data = json.loads(payload)
        if not isinstance(data, dict):
            return None
        return {
            "identity": {k: v for k, v in (data.get("identity") or {}).items() if v},
            "profile": {k: v for k, v in (data.get("profile") or {}).items() if v},
            "knowledge": [
                i for i in (data.get("knowledge") or [])
                if isinstance(i, dict) and i.get("text")
            ],
        }
    except Exception:  # noqa: BLE001 — fail open to heuristics
        logger.info("resume llm parse failed; using heuristics", exc_info=True)
        return None


def _github_get(path: str):
    """JSON from api.github.com, or ``{\"_status\": 404}``, or None on network failure."""
    import httpx

    s = get_settings()
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "JobPilot-Apply/1.0",
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


def _section_after(blob: str, heading: str, stop: str) -> str:
    m = re.search(heading, blob, re.I)
    if not m:
        return ""
    rest = blob[m.end():]
    nxt = re.search(stop, rest, re.I)
    return rest[: nxt.start() if nxt else 1500]


def _experience_from_text(blob: str) -> list[dict]:
    """Grab bullets under an EXPERIENCE heading. Jobs keep their location line."""
    section = _section_after(
        blob,
        r"(?:^|\n)\s*(?:professional experience|work experience|experience|employment)\s*\n",
        r"\n\s*(?:projects?|education|skills|key projects|selected projects)\s*\n",
    )
    if not section:
        return []
    items = []
    for line in section.splitlines():
        line = re.sub(r"^[\s\-\*•·]+", "", line).strip()
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
        r"(?:^|\n)\s*(?:projects?|selected projects|personal projects|key projects)\s*\n",
        r"\n\s*(?:experience|education|skills|work history|awards|professional experience)\s*\n",
    )
    if not section:
        return []
    items = []
    for line in section.splitlines():
        line = re.sub(r"^[\s\-\*•·]+", "", line).strip()
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
    m = _CITY_STATE_RE.search(raw)
    if m:
        return {"city": m.group(1), "state": m.group(2)}
    if "," in raw:
        city, rest = raw.split(",", 1)
        city, rest = city.strip(), rest.strip()
        out = {}
        if city:
            out["city"] = city
        if len(rest) == 2 and rest.isalpha():
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
