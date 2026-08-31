"""Decide which new postings are worth alerting about.

Two stages, cheapest first (the cost-control rule):

  1. ``prefilter`` — free. Drops postings whose title/description share no term
     with the profile's roles/keywords, so the LLM only ever sees plausible hits.
  2. ``score`` — assigns each survivor a 0..1 fit score. Uses Claude (Haiku) when
     a key is configured, batching every posting into ONE call; otherwise (and on
     any API error, and in tests) a free keyword/location heuristic. The heuristic
     is the path CI exercises, mirroring the router's design.

An empty profile scores a neutral 0.5 — below the default alert threshold — so
the user gets no spam until they say what they want.
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

logger = logging.getLogger("matcher")

_SPLIT = re.compile(r"[,/]| and ", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9+#.]+")
# Description chars sent to the LLM per posting — enough signal, few tokens.
_DESC_CHARS = 300

# Seniority / format / generic filler that shouldn't gate matching — these words
# appear in (or are absent from) postings independent of fit, so matching on them
# would either over- or under-filter.
_STOPWORDS = {
    "new", "grad", "graduate", "entry", "level", "junior", "senior", "staff",
    "lead", "principal", "role", "roles", "job", "jobs", "position", "positions",
    "opening", "openings", "remote", "hybrid", "onsite", "or", "and", "the", "a",
    "an", "for", "in", "at", "of", "to", "with", "looking", "want", "seeking",
    "near", "based",
}
# Abbreviations → extra substrings to also match on, since a bare "swe" never
# substring-matches "Software Engineer". Keeps the free pre-filter from starving
# the scorer on common shorthand.
_SYNONYMS = {
    # Map abbreviations to PRECISE phrases only — never a bare generic word like
    # "software" (it matches every software-company posting, incl. their sales
    # roles, and floods the per-tick scoring cap with false positives).
    "swe": ("software engineer",),
    "sde": ("software engineer",),
    "sdet": ("software engineer in test",),
    "ml": ("machine learning",),
    "ai": ("machine learning", "artificial intelligence"),
    "nlp": ("natural language",),
    "pm": ("product manager", "product management"),
    "tpm": ("technical program manager",),
    "frontend": ("front end", "front-end"),
    "backend": ("back end", "back-end"),
    "fullstack": ("full stack", "full-stack"),
    "devops": ("devops", "site reliability"),
    "sre": ("site reliability",),
    "qa": ("quality assurance",),
    "ux": ("user experience",),
    "ui": ("user interface",),
    "ds": ("data scientist", "data science"),
}
# Words too generic to gate matching ON THEIR OWN (they appear across unrelated
# roles, e.g. "software"/"engineer" in a software company's sales posting). They
# still count inside a precise multi-word phrase like "software engineer".
_GENERIC_TOKENS = {
    "software", "engineer", "engineering", "developer", "development", "manager",
    "management", "analyst", "specialist", "associate", "intern", "data", "tech",
    "technical", "applications", "systems", "platform",
}


def _intern_variants(phrase: str) -> set[str]:
    """A 'software engineer' profile should still see 'Software Engineering Intern'.

    Whole-word matching treats 'engineer' ≠ 'engineering', so intern/co-op titles
    would otherwise miss the scoring cap entirely.
    """
    if not phrase.endswith(" engineer"):
        return set()
    stem = phrase[: -len(" engineer")]
    return {
        f"{stem} engineering",
        f"{stem} engineer intern",
        f"{stem} engineering intern",
        f"{stem} engineering internship",
        f"{stem} engineering co-op",
        f"{stem} engineering coop",
    }


def _role_variants(phrase: str) -> set[str]:
    """Every spelling a posting might use for one profile role.

    The prefilter has always expanded these; scoring did not, and the gap was
    the bug. "Software Engineering Intern, Summer 2027" passed the gate on a
    "software intern" profile and then scored 0.15 — the same as an unrelated
    marketing role — because neither raw role string appears in that title
    verbatim. Surviving the filter only to be scored as noise is worse than
    being filtered out: it lands in the feed looking like a bad match.
    """
    out = {phrase}
    out |= _intern_variants(phrase)
    # The other direction: a profile that says "software intern" against a
    # posting that says "Software Engineering Intern".
    if phrase.endswith(" intern"):
        stem = phrase[: -len(" intern")]
        out |= {
            f"{stem} engineering intern",
            f"{stem} engineer intern",
            f"{stem} engineering internship",
            f"{stem} internship",
            f"{stem} engineering co-op",
            f"{stem} engineering coop",
        }
    return out


def _terms(profile: sqlite3.Row | None) -> set[str]:
    """Profile keyword *concepts* for relevance scoring (the heuristic ratio uses
    this as the denominator, so it stays close to what the user actually typed —
    one entry per comma/'and'-separated clause)."""
    if profile is None:
        return set()
    raw = f"{profile['roles'] or ''},{profile['keywords'] or ''}"
    return {t.strip().lower() for t in _SPLIT.split(raw) if t.strip()}


def _roles(profile: sqlite3.Row | None) -> set[str]:
    """Just the target roles. Scoring weights a role match in the *title* far
    above any single skill keyword; merged into ``_terms`` it was worth exactly
    as much as "docker"."""
    if profile is None:
        return set()
    return {t.strip().lower() for t in _SPLIT.split(profile["roles"] or "") if t.strip()}


def _match_terms(profile: sqlite3.Row | None) -> set[str]:
    """Broad term set for the free pre-filter gate: clause phrases + individual
    word tokens + expanded abbreviations (so a bare "swe" still surfaces
    "Software Engineer"). Deliberately looser than ``_terms`` — its only job is to
    avoid starving the scorer; the scorer makes the real call."""
    if profile is None:
        return set()
    raw = f"{profile['roles'] or ''} , {profile['keywords'] or ''}"
    terms: set[str] = set()
    for clause in _SPLIT.split(raw):
        words = _WORD.findall(clause.lower())
        sig = [w for w in words if w not in _STOPWORDS and len(w) >= 2]
        if len(sig) >= 2:
            phrase = " ".join(sig)  # e.g. "software engineer", "data scientist"
            terms.add(phrase)
            terms.update(_intern_variants(phrase))
        for w in sig:
            # Generic words only gate inside a phrase, never standalone (else
            # "software" matches every software-company posting incl. sales).
            if w not in _GENERIC_TOKENS and (len(w) >= 3 or w in _SYNONYMS):
                terms.add(w)
            extras = _SYNONYMS.get(w, ())
            terms.update(extras)
            for extra in extras:
                terms.update(_intern_variants(extra))
    return terms


def _locations(profile: sqlite3.Row | None) -> list[str]:
    if profile is None or not (profile["locations"] or "").strip():
        return []
    return [t.strip().lower() for t in _SPLIT.split(profile["locations"]) if t.strip()]


def _haystack(p: JobPosting) -> str:
    return f"{p.title} {p.location} {p.description}".lower()


def _term_in(term: str, text: str) -> bool:
    """Whole-token/phrase match — NOT a raw substring. Stops short terms like
    "ai"/"ml"/"swe" from matching inside unrelated words (email, html, answered)."""
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def prefilter(postings: list[JobPosting], profile: sqlite3.Row | None) -> list[JobPosting]:
    """Free gate: keep postings that mention any role/keyword term (whole-word).

    With no terms configured we can't cheaply tell signal from noise, so we pass
    everything through to scoring (which will return a neutral score).
    """
    terms = _match_terms(profile)
    if not terms:
        return list(postings)
    return [p for p in postings if any(_term_in(t, _haystack(p)) for t in terms)]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

# How many matched skill concepts read as full skill coverage. Without a cap,
# the score was `matched / len(terms)`, so every extra skill a user listed
# lowered the score of every job: the same "Senior Platform Engineer" posting
# scored 0.650 against a 4-term profile and 0.332 against an 11-term one. With
# the alert threshold at 0.6, thoroughly filling in your profile meant nothing
# surfaced at all.
_SKILL_SATURATION = 4
#: Weight on "is this my job title" vs "does it use my skills". At 0.5 a title
#: match with no skill overlap scores exactly 0.5 -- under the 0.6 alert bar.
_ROLE_WEIGHT = 0.5


def _coverage(skills: set[str], text: str) -> float:
    """How much of the skill list the posting mentions, saturating.

    Whole-word, like prefilter: plain ``t in text`` credited "ai" for "email"
    and "ml" for "HTML", the exact bug ``_term_in`` exists to stop. Saturating
    at ``_SKILL_SATURATION`` keeps a thorough profile from scoring *worse* --
    a plain ``matched / len(skills)`` ratio meant every skill someone added
    dragged every job's score down.
    """
    matched = sum(1 for t in skills if _term_in(t, text))
    return min(1.0, matched / min(len(skills), _SKILL_SATURATION))


def _heuristic_score(p: JobPosting, terms: set[str], locations: list[str],
                     *, roles: set[str] | frozenset[str] = frozenset()) -> float:
    text = _haystack(p)
    title = (p.title or "").lower()
    # Roles get their own axis below. Leaving them in the skill denominator too
    # counted each role twice, which let a title match with zero skill overlap
    # ("Software Engineer -- legacy maintenance", for a Kubernetes profile)
    # reach 0.70 and clear the alert threshold.
    skills = set(terms) - set(roles)
    # Match roles through their variants, exactly as the prefilter does. The two
    # disagreeing is what let intern postings through the gate and then scored
    # them as noise.
    role_forms = {v for r in roles for v in _role_variants(r)}
    if roles:
        in_title = any(_term_in(v, title) for v in role_forms)
        anywhere = in_title or any(_term_in(v, text) for v in role_forms)
        role_component = 1.0 if in_title else (0.45 if anywhere else 0.0)
        if skills:
            # A title match alone lands at _ROLE_WEIGHT -- deliberately just
            # under the 0.6 alert bar, so "right title, wrong stack" stays a
            # browse, not a notification. Skills carry it the rest of the way.
            base = (_ROLE_WEIGHT * role_component
                    + (1 - _ROLE_WEIGHT) * _coverage(skills, text))
        else:
            # Titles-only profile — the skills step is skippable, so this is a
            # normal state, not an edge case. There is no skill axis to weigh
            # the title against, and scoring on role presence alone gave every
            # same-titled posting an identical number: a real run produced three
            # distinct values across the whole feed and the top of it was a flat
            # tie. Title focus is the one signal left that is free and honest.
            base = (_ROLE_WEIGHT * role_component
                    + (1 - _ROLE_WEIGHT) * _title_focus(role_forms, title))
    elif skills:
        base = _coverage(skills, text)
    elif terms:
        base = _coverage(terms, text)
    else:
        base = 0.5
    if locations:
        if any(loc in text for loc in locations):
            base += 0.15
        elif "remote" in text:
            base += 0.05
        else:
            base -= 0.15
    return round(max(0.0, min(1.0, base)), 3)


def _title_focus(role_forms: set[str], title: str) -> float:
    """How much of the posting's title the matched role accounts for.

    Only used when the profile is titles-only. "Software Engineer" is a closer
    answer to a "software engineer" profile than "Software Engineer, ML
    Developer Experience, Ray Core" — the extra clauses are specialisation the
    person never asked for. Ranking on this is a claim about *closeness to the
    stated role*, which is all a titles-only profile has said.
    """
    words = _WORD.findall(title)
    if not words:
        return 0.0
    longest = max(
        (len(_WORD.findall(v)) for v in role_forms if _term_in(v, title)),
        default=0,
    )
    if not longest:
        return 0.0
    return min(1.0, longest / len(words))


_SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    # No minimum/maximum: Anthropic structured outputs reject
                    # numeric bounds. _llm_score clamps to [0,1] after parsing.
                    "score": {"type": "number"},
                },
                "required": ["id", "score"],
                # Anthropic structured outputs require this on every object,
                # else the request 400s and scoring silently falls back to
                # the heuristic (everything ends up ~0.15).
                "additionalProperties": False,
            },
        }
    },
    "required": ["scores"],
    "additionalProperties": False,
}

_llm_client = None
_llm_limiter: TokenBucket | None = None


def _get_llm():
    global _llm_client, _llm_limiter
    if _llm_client is None:
        from . import llm_health  # lazy: offline/test paths skip anthropic

        s = get_settings()
        _llm_client = llm_health.client(s.anthropic_api_key)
        _llm_limiter = TokenBucket(s.llm_rate_limit_per_min)
    return _llm_client, _llm_limiter


# Postings per LLM scoring call. Bounded so the JSON response never exceeds
# max_tokens — a too-large batch truncates the output and the whole parse fails
# (then the batch silently falls back to the heuristic, wasting the call).
_SCORE_CHUNK = 25


def _llm_score(postings: list[JobPosting], profile_block: str) -> dict[int, float]:
    """Score all postings, in bounded chunks, merged into one {index: score} map.
    Raises on failure (caller falls back to the heuristic)."""
    out: dict[int, float] = {}
    for start in range(0, len(postings), _SCORE_CHUNK):
        chunk = postings[start:start + _SCORE_CHUNK]
        for local_i, sc in _llm_score_chunk(chunk, profile_block).items():
            out[start + local_i] = sc  # offset back to the global index
    return out


def _llm_score_chunk(postings: list[JobPosting], profile_block: str) -> dict[int, float]:
    """One Claude call for up to ``_SCORE_CHUNK`` postings. Returns {index: score}."""
    client, limiter = _get_llm()
    if not limiter.allow():
        raise RuntimeError("llm rate limited")
    from . import llm_budget
    if not llm_budget.consume(feature="discovery"):
        raise RuntimeError("llm user daily cap")
    listing = "\n".join(
        f"[{i}] {p.title} — {p.location or 'n/a'} | {p.description[:_DESC_CHARS]}"
        for i, p in enumerate(postings)
    )
    system = (
        "You rate how well each job posting fits a candidate. Return a score from "
        "0 (irrelevant) to 1 (excellent fit) for every posting id. Weigh role/title "
        "match most, then seniority, then location preference."
    )
    user = f"CANDIDATE PROFILE:\n{profile_block}\n\nPOSTINGS:\n{listing}"
    resp = client.messages.create(
        model=get_settings().anthropic_model,
        # ~30 tokens/score (id + value + JSON); generous ceiling. With the chunk
        # cap this never truncates.
        max_tokens=min(2048, 80 + 30 * len(postings)),
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": _SCORE_SCHEMA}},
    )
    payload = next(b.text for b in resp.content if b.type == "text")
    out: dict[int, float] = {}
    for item in json.loads(payload).get("scores", []):
        try:
            out[int(item["id"])] = max(0.0, min(1.0, float(item["score"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def score(
    postings: list[JobPosting],
    profile: sqlite3.Row | None,
    *,
    llm=None,
    allow_llm: bool = True,
) -> list[tuple[JobPosting, float]]:
    """Return [(posting, score)] for each posting.

    Scoring strategy, in order of preference:
      1. LLM (Haiku) when keyed (unless ``allow_llm=False``).
      2. Free keyword/location heuristic (the CI path, and the final fallback).
    Each layer degrades to the next on failure, so discovery never blocks.
    ``allow_llm=False`` skips the paid scorer — the interactive swipe deck sets
    it False, so its score is the free heuristic (a fine sort key) with no API
    latency; the LLM budget goes to card summaries. ``llm`` injects a scorer in
    tests.
    """
    if not postings:
        return []
    terms, locations = _terms(profile), _locations(profile)
    roles = _roles(profile)

    use_llm = llm is not None or (allow_llm and get_settings().use_llm_router)
    if use_llm:
        try:
            scorer = llm or _llm_score
            llm_scores = scorer(postings, profile_text(profile))
            return [
                (p, round(float(llm_scores[i]), 3))
                if i in llm_scores
                else (p, _heuristic_score(p, terms, locations, roles=roles))
                for i, p in enumerate(postings)
            ]
        except Exception:  # noqa: BLE001 — never block discovery on the LLM
            logger.warning("LLM scoring failed; using heuristic", exc_info=True)
    return [(p, _heuristic_score(p, terms, locations, roles=roles)) for p in postings]
