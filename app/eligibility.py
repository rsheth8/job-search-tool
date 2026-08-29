"""Eligibility / qualification gate (Matching v2).

The matcher asks "is this a good *fit*?"; this layer asks the blunter question
**"could this candidate realistically do — or even be considered for — this job
at all?"** It exists because discovery surfaces roles an entry-level CS/DS
student plainly can't land (Senior/Staff/Manager titles, "8+ years required",
licensed professions), which waste attention and application effort.

**Rule tier only (free, on by default).** Profile-driven. Disqualifies on a clear
*seniority gap* (role much more senior than the candidate), a big *years-of-
experience* requirement, or a *hard credential* the candidate lacks
(nursing/CPA/clearance/required doctorate). The field gate and license check
apply when the profile looks technical (or is empty — the product default is
entry CS); marketing/HR/nursing profiles skip those so the right jobs survive.
Deliberately conservative — it only drops on clear over-requirement, never on
a borderline call.

"Candidate level" is read from the profile's seniority/background, falling back
to ``eligibility_candidate_level`` so it works before a profile is filled in.
"""
from __future__ import annotations

import re
import sqlite3

from .config import get_settings
from .jobsources import JobPosting

# Seniority ladder. Higher = more senior. Used for both candidate and role.
#   1 entry/new-grad/intern/junior   2 mid (II/III)   3 senior/lead/manager
#   4 staff/principal/director/VP/chief
_LEVEL = {
    "entry": 1, "new grad": 1, "new-grad": 1, "newgrad": 1, "student": 1,
    "intern": 1, "internship": 1, "junior": 1, "jr": 1, "associate": 1,
    "early career": 1, "apprentice": 1, "trainee": 1, "graduate": 1, "grad": 1,
    "mid": 2, "intermediate": 2, "mid-level": 2,
    "senior": 3, "sr": 3, "lead": 3, "manager": 3, "mgr": 3, "architect": 3,
    "staff": 4, "principal": 4, "director": 4, "vp": 4, "vice president": 4,
    "head of": 4, "chief": 4, "distinguished": 4, "fellow": 4,
}
# Role-title tokens, scanned whole-word, mapped to a rank. Order doesn't matter;
# we take the max rank found. Unmarked titles default to mid (2), so a plain
# "Software Engineer" is allowed for an entry candidate (gap of 1).
_ROLE_TOKENS = {
    r"\bprincipal\b": 4, r"\bstaff\b": 4, r"\bdirector\b": 4, r"\bvp\b": 4,
    r"\bvice president\b": 4, r"\bhead of\b": 4, r"\bchief\b": 4,
    r"\bdistinguished\b": 4, r"\bfellow\b": 4,
    r"\bsenior\b": 3, r"\bsr\.?\b": 3, r"\blead\b": 3, r"\bmanager\b": 3,
    r"\bmgr\b": 3, r"\barchitect\b": 3,
    r"\bii\b": 2, r"\biii\b": 2, r"\biv\b": 2, r"\bmid\b": 2,
    r"\bjunior\b": 1, r"\bjr\.?\b": 1, r"\bassociate\b": 1, r"\bentry\b": 1,
    r"\bintern\b": 1, r"\bapprentice\b": 1, r"\btrainee\b": 1,
}
_ROLE_DEFAULT_RANK = 2

_YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:years?|yrs?|yoe)\b", re.I)

# Field gate (for a technical candidate). Applied only when the *profile*
# looks technical (or is empty — this app's default is entry CS). A title in
# a clearly NON-technical field is dropped — UNLESS it also carries a technical
# signal (e.g. "Sales Engineer", "Marketing Analyst"), which keeps adjacent roles.
_TECH_TITLE_RE = re.compile(
    r"\b(engineer|engineering|developer|programmer|software|swe|sde|sdet|"
    r"scientist|data|analyst|analytics|machine learning|\bml\b|\bai\b|"
    r"artificial intelligence|deep learning|nlp|devops|\bsre\b|infrastructure|"
    r"platform|backend|back-end|frontend|front-end|full[ -]?stack|security|"
    r"research|computer|systems|database|cloud|mobile|ios|android|\bqa\b|"
    r"technical|\bit\b|robotics|hardware|firmware|quantitative|\bquant\b)\b",
    re.I,
)
_NONTECH_TITLE_RE = re.compile(
    r"\b(account executive|account exec|business development|sales development|"
    r"\bsales\b|\bsdr\b|\bbdr\b|account manager|recruiter|recruiting|"
    r"talent acquisition|sourcer|marketing|\bbrand\b|copywriter|content writer|"
    r"content strategist|social media|community manager|customer success|"
    r"customer support|customer experience|administrative|executive assistant|"
    r"receptionist|office manager|accountant|accounting|bookkeeper|payroll|"
    r"accounts payable|accounts receivable|auditor|paralegal|attorney|counsel|"
    r"\bnurse\b|clinical|therapist|physician|teacher|human resources|\bhr\b|"
    r"people operations|merchandiser|buyer|underwriter|loan officer|"
    r"administrative coordinator|operations coordinator|talent community|"
    r"talent pool|talent network|\bexaminer\b|\bdriver\b)\b",
    re.I,
)

# Hard credentials a CS/DS candidate generally won't hold. Matched only when the
# posting says they're *required* (we avoid "preferred"/"a plus"). High-precision.
_CREDENTIAL_RE = re.compile(
    r"(registered nurse|nursing license|\brn\b license|certified public accountant|"
    r"\bcpa\b|active(?:\s+\w+){0,2}\s+clearance|security clearance|ts/sci|"
    r"\bcdl\b|commercial driver|real estate license|professional engineer license|"
    r"\bpe license\b|medical degree|bar admission|admitted to the bar)",
    re.I,
)
_DEGREE_REQ_RE = re.compile(
    r"(ph\.?d\.?|doctorate|md|m\.d\.)\s+(?:is\s+)?required|"
    r"requires?\s+(?:a\s+)?(?:ph\.?d\.?|doctorate|md)",
    re.I,
)


def _rank_from_text(text: str) -> int | None:
    """Highest seniority level named in free text, or None if none found."""
    t = f" {text.lower()} "
    best = None
    for word, rank in _LEVEL.items():
        if re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", t):
            best = rank if best is None else max(best, rank)
    return best


def _profile_blob(profile: sqlite3.Row | None) -> str:
    if profile is None:
        return ""
    parts: list[str] = []
    for field in ("roles", "keywords", "seniority", "resume_summary"):
        try:
            parts.append(profile[field] or "")
        except (IndexError, KeyError):
            pass
    return " ".join(parts)


def profile_looks_technical(profile: sqlite3.Row | None) -> bool:
    """True when the field/credential gate should treat this as a tech search.

    Empty / missing profile → technical (the product default is entry CS).
    A tech signal in roles/keywords wins. A non-tech signal with no tech
    signal means skip the field gate so marketing/HR/nursing searches work.
    Ambiguous (e.g. product manager) stays technical.
    """
    blob = _profile_blob(profile)
    if not blob.strip():
        return True
    if _TECH_TITLE_RE.search(blob):
        return True
    if _NONTECH_TITLE_RE.search(blob):
        return False
    return True


def candidate_rank(profile: sqlite3.Row | None) -> int:
    """The candidate's seniority level from their profile, else the configured
    fallback (default 'entry' -> 1)."""
    if profile is not None:
        for field in ("seniority", "roles", "resume_summary"):
            try:
                rank = _rank_from_text(profile[field] or "")
            except (IndexError, KeyError):
                rank = None
            if rank is not None:
                return rank
    return _rank_from_text(get_settings().eligibility_candidate_level) or 1


def _role_rank(title: str) -> int:
    title_l = f" {(title or '').lower()} "
    best = None
    for pattern, rank in _ROLE_TOKENS.items():
        if re.search(pattern, title_l):
            best = rank if best is None else max(best, rank)
    return best if best is not None else _ROLE_DEFAULT_RANK


def _max_years(text: str) -> int:
    return max((int(m.group(1)) for m in _YEARS_RE.finditer(text or "")), default=0)


def rule_reasons(posting: JobPosting, profile: sqlite3.Row | None) -> list[str]:
    """Disqualifying reasons under the free rule tier; empty == eligible."""
    cand = candidate_rank(profile)
    reasons: list[str] = []

    role = _role_rank(posting.title or "")
    if role - cand >= 2:
        reasons.append(f"role seniority ({role}) far above candidate ({cand})")

    # Years required scales with level: entry tolerates <4, each level +3 more.
    years_cap = 4 + (cand - 1) * 3
    yrs = _max_years(f"{posting.title} {posting.description}")
    if yrs >= years_cap:
        reasons.append(f"requires {yrs}+ years (cap {years_cap})")

    desc = posting.description or ""
    technical = profile_looks_technical(profile)
    # RN/CPA/clearance/etc. only gate technical (or default) candidates.
    if technical and (
        _CREDENTIAL_RE.search(desc) or _CREDENTIAL_RE.search(posting.title or "")
    ):
        reasons.append("requires a hard credential/license")
    if _DEGREE_REQ_RE.search(desc):
        reasons.append("requires an advanced degree")

    # Field gate: a clearly non-technical role for a technical candidate, with no
    # technical signal in the title (so "Sales Engineer"/"Data Analyst" survive).
    title = posting.title or ""
    if (get_settings().eligibility_field_filter
            and technical
            and _NONTECH_TITLE_RE.search(title)
            and not _TECH_TITLE_RE.search(title)):
        reasons.append("role is outside the candidate's (technical) field")
    return reasons


def is_eligible(posting: JobPosting, profile: sqlite3.Row | None) -> bool:
    return not rule_reasons(posting, profile)


def filter_eligible(
    postings: list[JobPosting], profile: sqlite3.Row | None
) -> tuple[list[JobPosting], int]:
    """Rule tier: return (kept, dropped_count)."""
    kept = [p for p in postings if is_eligible(p, profile)]
    return kept, len(postings) - len(kept)
