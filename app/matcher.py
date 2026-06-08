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

from . import embeddings
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


def _terms(profile: sqlite3.Row | None) -> set[str]:
    """Profile keyword *concepts* for relevance scoring (the heuristic ratio uses
    this as the denominator, so it stays close to what the user actually typed —
    one entry per comma/'and'-separated clause)."""
    if profile is None:
        return set()
    raw = f"{profile['roles'] or ''},{profile['keywords'] or ''}"
    return {t.strip().lower() for t in _SPLIT.split(raw) if t.strip()}


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
            terms.add(" ".join(sig))  # e.g. "software engineer", "data scientist"
        for w in sig:
            # Generic words only gate inside a phrase, never standalone (else
            # "software" matches every software-company posting incl. sales).
            if w not in _GENERIC_TOKENS and (len(w) >= 3 or w in _SYNONYMS):
                terms.add(w)
            terms.update(_SYNONYMS.get(w, ()))
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

def _heuristic_score(p: JobPosting, terms: set[str], locations: list[str]) -> float:
    text = _haystack(p)
    if not terms:
        base = 0.5
    else:
        matched = sum(1 for t in terms if t in text)
        base = matched / len(terms)
    if locations:
        if any(loc in text for loc in locations):
            base += 0.15
        elif "remote" in text:
            base += 0.05
        else:
            base -= 0.15
    return round(max(0.0, min(1.0, base)), 3)


# Chars of description fed to the embedder. Generous vs the LLM's _DESC_CHARS:
# embeddings handle longer context cheaply and descriptions are already capped
# at MAX_DESCRIPTION_CHARS upstream.
_EMBED_DESC_CHARS = 1500


def _embed_text(p: JobPosting) -> str:
    return f"{p.title}\n{p.location}\n{p.description[:_EMBED_DESC_CHARS]}".strip()


def _embedding_score(
    postings: list[JobPosting],
    profile: sqlite3.Row | None,
    embedder,
    terms: set[str],
    locations: list[str],
) -> list[tuple[JobPosting, float]] | None:
    """Score by cosine similarity (profile vs each JD).

    Returns None to signal "embeddings unavailable, fall back" — when the profile
    is empty (nothing to compare against) or the profile vector can't be built.
    A *per-posting* embed miss falls back to the heuristic for that posting only,
    mirroring how the LLM path fills gaps. Each posting's vector is stashed on the
    object so ``save_posting`` persists it (embed once).

    NOTE: cosine magnitudes don't line up 1:1 with the keyword heuristic's scale,
    so ``job_relevance_threshold`` may want re-tuning (the user already has TUNE)
    after embeddings go live. Calibration is a deliberate follow-up, not done here.
    """
    profile_block = profile_text(profile)
    if not profile_block:
        return None
    pvecs = embedder([profile_block], input_type="query")
    if not pvecs or pvecs[0] is None:
        return None
    pvec = pvecs[0]
    dvecs = embedder([_embed_text(p) for p in postings], input_type="document")
    out: list[tuple[JobPosting, float]] = []
    for p, dvec in zip(postings, dvecs):
        if dvec is None:
            out.append((p, _heuristic_score(p, terms, locations)))
            continue
        p.embedding = dvec
        base = max(0.0, embeddings.cosine(pvec, dvec))
        if locations:
            text = _haystack(p)
            if any(loc in text for loc in locations):
                base += 0.05
            elif "remote" in text:
                base += 0.02
            else:
                base -= 0.05
        out.append((p, round(max(0.0, min(1.0, base)), 3)))
    return out


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
        import anthropic  # lazy: offline/test paths never import it

        s = get_settings()
        _llm_client = anthropic.Anthropic(api_key=s.anthropic_api_key)
        _llm_limiter = TokenBucket(s.llm_rate_limit_per_min)
    return _llm_client, _llm_limiter


def _llm_score(postings: list[JobPosting], profile_block: str) -> dict[int, float]:
    """Score every posting in one Claude call. Returns {index: score}. Raises on failure."""
    client, limiter = _get_llm()
    if not limiter.allow():
        raise RuntimeError("llm rate limited")
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
        max_tokens=min(1024, 40 + 12 * len(postings)),
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
    embedder=None,
) -> list[tuple[JobPosting, float]]:
    """Return [(posting, score)] for each posting.

    Scoring strategy, in order of preference:
      1. Embeddings (cosine profile-vs-JD) when active or injected — semantic.
      2. LLM (Haiku) when keyed.
      3. Free keyword/location heuristic (the CI path, and the final fallback).
    Each layer degrades to the next on failure, so discovery never blocks.
    ``llm`` / ``embedder`` inject scorers in tests.
    """
    if not postings:
        return []
    terms, locations = _terms(profile), _locations(profile)

    embed_fn = embedder or (embeddings.embed if get_settings().embedding_active else None)
    if embed_fn is not None:
        result = _embedding_score(postings, profile, embed_fn, terms, locations)
        if result is not None:
            return result
        # Profile/embedder gave nothing usable — fall through to LLM/heuristic.

    use_llm = llm is not None or get_settings().use_llm_router
    if use_llm:
        try:
            scorer = llm or _llm_score
            llm_scores = scorer(postings, profile_text(profile))
            return [
                (p, round(float(llm_scores[i]), 3))
                if i in llm_scores
                else (p, _heuristic_score(p, terms, locations))
                for i, p in enumerate(postings)
            ]
        except Exception:  # noqa: BLE001 — never block discovery on the LLM
            logger.warning("LLM scoring failed; using heuristic", exc_info=True)
    return [(p, _heuristic_score(p, terms, locations)) for p in postings]
