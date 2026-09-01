"""Sector-tagged company catalog — references, not a live scrape list.

``data/company_catalog.json`` holds employers by job-search field (hospitals,
universities, listed companies, plus any ATS board we can actually poll).
Discovery only *probes* entries that have a public Greenhouse / Lever / Ashby /
Workable / SmartRecruiters token; the rest are name references for
``track openings at …`` resolution and future growth.

The catalog is the source of truth on disk (not copied into SQLite on every
test DB) so ticks stay cheap.
"""
from __future__ import annotations

import json
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

from .config import get_settings

SECTORS = (
    "software", "healthcare", "science", "finance", "retail", "education",
    "manufacturing", "energy", "media", "hospitality", "legal", "government",
    "nonprofit", "consulting", "marketing", "logistics", "insurance",
    "real_estate", "hr", "design", "sales", "product", "support",
    "construction", "aerospace", "aviation", "automotive", "telecom",
    "architecture", "gaming", "sports", "fitness", "veterinary",
)

# Employer *industries* — directory rotation should stay inside these.
# Occupation fields (sales, marketing, HR, design, product, support) hire
# across companies, so directory_sectors() still includes software boards.
_EMPLOYER_INDUSTRIES = frozenset({
    "healthcare", "education", "government", "legal", "construction",
    "aerospace", "aviation", "automotive", "hospitality", "nonprofit", "science",
    "energy", "telecom", "manufacturing", "veterinary", "architecture",
    "logistics", "insurance", "real_estate", "fitness", "sports", "gaming",
})

_PROFILE_SECTORS: list[tuple[re.Pattern, tuple[str, ...]]] = [
    (re.compile(
        r"\b(nurse|nursing|clinical|hospital|physician|therapist|healthcare|"
        r"health care|registered nurse|\brn\b|home health|hospice|"
        r"nursing home|cna|lpn)\b", re.I
    ), ("healthcare",)),
    (re.compile(
        r"\b(biotech|pharma|pharmaceutical|life science|scientist|laboratory|"
        r"chemist|research associate)\b", re.I
    ), ("science",)),
    (re.compile(
        r"\b(teacher|professor|education|university|school|academic|principal|"
        r"school district|k-12|k12)\b",
        re.I,
    ), ("education",)),
    (re.compile(
        r"\b(product manager|product management|product owner|\bpm\b)\b", re.I
    ), ("product", "software")),
    (re.compile(
        r"\b(customer success|customer support|customer experience|"
        r"support specialist|help desk|call center)\b", re.I
    ), ("support", "software")),
    (re.compile(
        r"\b(marketing|brand|copywriter|content strategist|social media)\b", re.I
    ), ("marketing", "media")),
    (re.compile(
        r"\b(sales|sdr|bdr|account executive|business development)\b", re.I
    ), ("sales", "marketing")),
    (re.compile(
        r"\b(accountant|accounting|finance|bookkeep|payroll|fp&a|controller)\b",
        re.I,
    ), ("finance",)),
    (re.compile(
        r"\b(ui/ux|ux/ui|product design|graphic design|designer|design systems|"
        r"ux|ui)\b", re.I
    ), ("design", "media")),
    (re.compile(
        r"\b(recruiter|recruiting|talent acquisition|human resources|\bhr\b|"
        r"people operations)\b", re.I
    ), ("hr",)),
    (re.compile(r"\b(attorney|counsel|paralegal|legal)\b", re.I), ("legal",)),
    (re.compile(r"\b(retail|merchandis|store manager|buyer)\b", re.I), ("retail",)),
    (re.compile(
        r"\b(hospitality|hotel|restaurant|food service|chef)\b", re.I
    ), ("hospitality",)),
    (re.compile(r"\b(consulting|consultant)\b", re.I), ("consulting",)),
    (re.compile(r"\b(nonprofit|non-profit|ngo|foundation)\b", re.I), ("nonprofit",)),
    (re.compile(
        r"\b(government|federal|civil service|public sector)\b", re.I
    ), ("government",)),
    (re.compile(
        r"\b(logistics|supply chain|warehouse|trucking|freight)\b", re.I
    ), ("logistics",)),
    (re.compile(r"\b(insurance|underwriter|actuary)\b", re.I), ("insurance",)),
    (re.compile(
        r"\b(construction|electrician|plumber|carpenter|welder|trades|"
        r"general contractor|superintendent|estimator)\b", re.I
    ), ("construction",)),
    (re.compile(
        r"\b(aerospace|defense|space systems|satellite|spacecraft)\b",
        re.I,
    ), ("aerospace",)),
    (re.compile(
        r"\b(aviation|airline|airport|pilot|flight attendant|cabin crew|"
        r"a&p|aircraft mechanic|air traffic|ramp agent|dispatcher|"
        r"flight school|avionics)\b",
        re.I,
    ), ("aviation", "aerospace")),
    (re.compile(
        r"\b(automotive|auto manufacturing|vehicle|ev manufacturing)\b", re.I
    ), ("automotive",)),
    (re.compile(
        r"\b(telecom|telecommunications|wireless|cable operator)\b", re.I
    ), ("telecom",)),
    (re.compile(
        r"\b(architect|architecture|civil engineer|structural engineer|"
        r"urban planner)\b", re.I
    ), ("architecture", "construction")),
    (re.compile(
        r"\b(game designer|game developer|game studio|video game)\b", re.I
    ), ("gaming", "software")),
    (re.compile(
        r"\b(sports|athletic|ncaa|league operations)\b", re.I
    ), ("sports",)),
    (re.compile(
        r"\b(personal trainer|fitness|gym|wellness coach)\b", re.I
    ), ("fitness",)),
    (re.compile(
        r"\b(veterinar|vet tech|animal hospital|veterinary)\b", re.I
    ), ("veterinary",)),
    (re.compile(
        r"\b(utility|lineman|power plant|renewable)\b", re.I
    ), ("energy",)),
    (re.compile(
        r"\b(software|engineer|developer|programmer|swe|sde|devops|sre)\b", re.I
    ), ("software",)),
]

_NAME_STRIP = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|company|co|"
    r"the|common stock|capital stock|class [a-z]|ordinary shares|"
    r"american depositary|ads|plc)\b",
    re.I,
)


def _catalog_path() -> Path:
    path = Path(get_settings().job_company_catalog_path)
    if not path.is_absolute():
        root = Path(__file__).resolve().parents[1]
        path = root / path
    return path


def _empty() -> dict:
    return {"version": 1, "boards": [], "names": {}}


@lru_cache(maxsize=1)
def load() -> dict:
    path = _catalog_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    boards = data.get("boards") or []
    names = data.get("names") or {}
    if not isinstance(boards, list):
        boards = []
    if not isinstance(names, dict):
        names = {}
    return {"version": int(data.get("version") or 1), "boards": boards, "names": names}


def reset_cache() -> None:
    load.cache_clear()
    # Anything derived from load() has to go with it, or a test pointed at a
    # different catalog file reads the previous one's index.
    _names_by_board.cache_clear()


def sectors_for_profile(profile: sqlite3.Row | None) -> frozenset[str]:
    """Job-search fields to rotate / look up from the catalog."""
    from .eligibility import _profile_blob, profile_looks_technical

    blob = _profile_blob(profile)
    hits: list[str] = []
    if blob.strip():
        for rx, sectors in _PROFILE_SECTORS:
            if rx.search(blob):
                hits.extend(sectors)
    if hits:
        return frozenset(hits)
    if profile_looks_technical(profile):
        return frozenset({"software"})
    return frozenset({"software"})


def directory_sectors(profile: sqlite3.Row | None) -> frozenset[str]:
    """Sectors whose ATS boards should rotate for this profile.

    Industry searches (nurse, teacher, construction) stay inside that
    industry. Occupation searches (sales, marketing, PM, support) still
    rotate software boards, where those roles are hired.
    """
    hits = sectors_for_profile(profile)
    if hits & _EMPLOYER_INDUSTRIES:
        return hits
    return hits | {"software"}


def _norm_name(name: str) -> str:
    t = (name or "").lower()
    t = _NAME_STRIP.sub(" ", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


def _board_key(source: str, token: str) -> tuple[str, str]:
    src = (source or "").strip().lower()
    tok = (token or "").strip()
    if src != "smartrecruiters":
        tok = tok.lower()
    return (src, tok)


def sector_index() -> dict[tuple[str, str], set[str]]:
    """``(source, token)`` → sectors for every catalog board with a token."""
    out: dict[tuple[str, str], set[str]] = {}
    for row in load().get("boards") or []:
        if not isinstance(row, dict):
            continue
        src = (row.get("source") or "").strip().lower()
        token = (row.get("token") or "").strip()
        if not src or not token:
            continue
        key = _board_key(src, token)
        secs = {str(s).strip().lower() for s in (row.get("sectors") or []) if s}
        out.setdefault(key, set()).update(secs or {"software"})
    return out


def probe_pairs(sectors: frozenset[str] | None) -> list[tuple[str, str]]:
    """Live ATS boards whose sectors intersect ``sectors`` (or all if None)."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in load().get("boards") or []:
        if not isinstance(row, dict):
            continue
        src = (row.get("source") or "").strip().lower()
        token = (row.get("token") or "").strip()
        if not src or not token:
            continue
        key = _board_key(src, token)
        if key in seen:
            continue
        secs = {str(s).strip().lower() for s in (row.get("sectors") or []) if s}
        if sectors and not (secs & set(sectors)):
            continue
        seen.add(key)
        out.append((src, token if src == "smartrecruiters" else token.lower()))
    return out


@lru_cache(maxsize=1)
def _names_by_board() -> dict[tuple[str, str], str]:
    index: dict[tuple[str, str], str] = {}
    for row in load().get("boards") or []:
        if not isinstance(row, dict):
            continue
        src = (row.get("source") or "").strip().lower()
        token = (row.get("token") or "").strip()
        name = (row.get("name") or "").strip()
        if src and token and name:
            index.setdefault((src, token.lower()), name)
    return index


def display_name(source: str, token: str) -> str | None:
    """The company's real name for an ATS board, if the catalog knows it.

    Titlecasing the token gives "Janestreet" and "Xai". The catalog already
    carries the human name for every board it lists, so anything deriving a
    company name from a URL should ask here first.
    """
    return _names_by_board().get(
        ((source or "").strip().lower(), (token or "").strip().lower()))


def lookup_board(company_name: str) -> dict | None:
    """Known ATS board for a company name, if the catalog has a token."""
    want = _norm_name(company_name)
    if not want:
        return None
    for row in load().get("boards") or []:
        if not isinstance(row, dict):
            continue
        src = (row.get("source") or "").strip().lower()
        token = (row.get("token") or "").strip()
        name = (row.get("name") or "").strip()
        if not src or not token or not name:
            continue
        if _norm_name(name) == want or _norm_name(token) == want:
            return {
                "source": src,
                "board_token": token if src == "smartrecruiters" else token.lower(),
                "company_name": name,
            }
    return None


def names_for_sectors(sectors: frozenset[str] | None) -> list[str]:
    """Employer names in ``sectors``, de-duped, stable order (sector then name)."""
    names = load().get("names") or {}
    want = {s.strip().lower() for s in (sectors or ()) if s}
    out: list[str] = []
    seen: set[str] = set()
    keys = sorted(want) if want else sorted(
        k for k, v in names.items() if isinstance(v, list)
    )
    for sector in keys:
        for raw in names.get(sector) or []:
            name = str(raw).strip()
            key = name.lower()
            if not name or key in seen:
                continue
            seen.add(key)
            out.append(name)
    return out


def name_count(sector: str | None = None) -> int:
    names = load().get("names") or {}
    if sector:
        return len(names.get(sector) or [])
    return sum(len(v) for v in names.values() if isinstance(v, list))


def stats() -> dict:
    data = load()
    names = data.get("names") or {}
    by_sector = {k: len(v) for k, v in names.items() if isinstance(v, list)}
    return {
        "boards": len(data.get("boards") or []),
        "names": sum(by_sector.values()),
        "by_sector": by_sector,
    }
