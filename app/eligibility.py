"""Eligibility / qualification gate (Matching v2).

The matcher asks "is this a good *fit*?"; this layer asks the blunter question
**"could this candidate realistically do — or even be considered for — this job
at all?"** It exists because discovery surfaces roles an entry-level CS/DS
student plainly can't land (Senior/Staff/Manager titles, "8+ years required",
licensed professions), which waste attention and application effort.

Two tiers, cheapest first (the house cost-control rule):

  1. **Rule tier (free, on by default).** Profile-driven. Disqualifies on a clear
     *seniority gap* (role much more senior than the candidate), a big *years-of-
     experience* requirement, or a *hard credential* the candidate lacks
     (nursing/CPA/clearance/required doctorate). Deliberately conservative — it
     only drops on clear over-requirement, never on a borderline call.
  2. **LLM tier (off by default; batched Haiku).** A nuanced "is this candidate
     plausibly qualified to apply?" judgement, customized by the profile. Gated +
     daily-capped, and **fail-open**: any error keeps the posting (we never bury
     roles because the model hiccuped).

"Candidate level" is read from the profile's seniority/background, falling back
to ``eligibility_candidate_level`` so it works before a profile is filled in.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3

from .config import get_settings
from .jobsources import JobPosting
from .profile import profile_text
from .ratelimit import TokenBucket

logger = logging.getLogger("eligibility")

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

_YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b", re.I)

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
    if _CREDENTIAL_RE.search(desc) or _CREDENTIAL_RE.search(posting.title or ""):
        reasons.append("requires a hard credential/license")
    if _DEGREE_REQ_RE.search(desc):
        reasons.append("requires an advanced degree")
    return reasons


def is_eligible(posting: JobPosting, profile: sqlite3.Row | None) -> bool:
    return not rule_reasons(posting, profile)


def filter_eligible(
    postings: list[JobPosting], profile: sqlite3.Row | None
) -> tuple[list[JobPosting], int]:
    """Rule tier: return (kept, dropped_count)."""
    kept = [p for p in postings if is_eligible(p, profile)]
    return kept, len(postings) - len(kept)


# ---------------------------------------------------------------------------
# LLM tier (optional)
# ---------------------------------------------------------------------------

_llm_client = None
_llm_limiter: TokenBucket | None = None

_ELIG_SCHEMA = {
    "type": "object",
    "properties": {
        "assessments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "qualified": {"type": "boolean"},
                },
                "required": ["id", "qualified"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["assessments"],
    "additionalProperties": False,
}


def reset_for_tests() -> None:
    global _llm_client, _llm_limiter
    _llm_client = None
    _llm_limiter = None


def _get_llm():
    global _llm_client, _llm_limiter
    if _llm_client is None:
        import anthropic

        s = get_settings()
        _llm_client = anthropic.Anthropic(api_key=s.anthropic_api_key)
        _llm_limiter = TokenBucket(s.llm_rate_limit_per_min)
    return _llm_client, _llm_limiter


def assess_batch(postings: list[JobPosting], profile_block: str) -> dict[int, bool]:
    """Ask Haiku, in one call, whether the candidate is plausibly qualified for
    each posting. Returns {index: qualified}. Raises on failure (caller fails open)."""
    client, limiter = _get_llm()
    if not limiter.allow():
        raise RuntimeError("llm rate limited")
    listing = "\n".join(
        f"[{i}] {p.title} — {(p.description or '')[:300]}"
        for i, p in enumerate(postings)
    )
    system = (
        "You judge whether a candidate could realistically APPLY to and be "
        "considered for each job — NOT whether it's a perfect fit. Mark "
        "qualified=true for roles whose hard requirements (years of experience, "
        "degree, license, domain) this candidate plausibly meets or could be "
        "considered for, including adjacent roles they could do. Mark "
        "qualified=false only when the role clearly demands seniority, credentials, "
        "or experience the candidate lacks. Be inclusive; reserve false for clear "
        "over-requirement."
    )
    user = f"CANDIDATE:\n{profile_block}\n\nJOBS:\n{listing}"
    resp = client.messages.create(
        model=get_settings().anthropic_model,
        max_tokens=min(1024, 40 + 10 * len(postings)),
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": _ELIG_SCHEMA}},
    )
    payload = next(b.text for b in resp.content if b.type == "text")
    out: dict[int, bool] = {}
    for item in json.loads(payload).get("assessments", []):
        try:
            out[int(item["id"])] = bool(item["qualified"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def filter_eligible_llm(
    postings: list[JobPosting], profile: sqlite3.Row | None, *, assess=None
) -> tuple[list[JobPosting], int]:
    """LLM tier: drop postings the model deems clearly unqualified. Fail-open —
    on no-key/disabled/cap/rate-limit/error it keeps everything. ``assess`` injects
    the judge in tests."""
    s = get_settings()
    active = assess is not None or (s.eligibility_llm_enabled and s.use_llm_router)
    if not active or not postings:
        return postings, 0
    if assess is None:
        from . import jobstore

        if not jobstore.allow_eligibility_call():
            logger.info("eligibility daily cap reached — keeping all")
            return postings, 0
    try:
        judge = assess or assess_batch
        verdicts = judge(postings, profile_text(profile))
    except Exception:  # noqa: BLE001 — never drop roles because the LLM failed
        logger.warning("eligibility LLM failed; keeping all", exc_info=True)
        return postings, 0
    if assess is None:
        from . import jobstore

        jobstore.record_eligibility_call()
    # Unknown ids default to qualified (keep) — fail-open per posting too.
    kept = [p for i, p in enumerate(postings) if verdicts.get(i, True)]
    return kept, len(postings) - len(kept)
