"""Plain-language TL;DR insights for swipe cards (credit-efficient).

Each card gets a one-line "what this role actually is" plus a short "is it for
you?" note, so the user can judge a posting at a glance instead of reading a wall
of JD text.

Built to spend as few AI credits as possible:
  * **Batched** — ONE Haiku call summarizes a whole deck (~15 roles), not one per
    card. (`max_tokens` scales with the batch size.)
  * **Cached** — every summary is stored by posting (`posting_summaries`), so a
    role is summarized once *ever*; re-fetched/re-shown cards are free.
  * **Cheap inputs** — only title + company + a short description slice are sent.
  * **Gated + daily-capped + fail-open** — off unless enabled & keyed; over the
    daily cap or on any error it simply returns cards without a TL;DR (the UI
    falls back to the description). Never blocks the deck.
"""
from __future__ import annotations

import json
import logging

from .config import get_settings
from .db import connect
from .ratelimit import TokenBucket

logger = logging.getLogger("insights")

# Bigger JD slice than before — JDs front-load boilerplate, so 320 chars often cut
# off the actual responsibilities/requirements and made summaries vague. ~800 is
# still cheap on Haiku, especially batched + cached.
_DESC_CHARS = 800
# Display fields the model returns and the UI renders, in order.
#   about  — what the company does       tldr   — what you'd do in this role
#   level  — experience targeted          skills — key tech named
#   fit    — honest read for the candidate
_FIELDS = ("about", "tldr", "level", "skills", "fit")

# Numeric 0-1 LLM judgements consumed by the re-ranker as features (cached per
# posting). Each has a neutral default for postings the LLM hasn't assessed.
#   fit_score    — overall fit + landability   tech_overlap — your skills ∩ the role's
#   stretch      — how much of a reach (0 easy ... 1 big stretch)
LLM_FEATURES = ("fit_score", "tech_overlap", "stretch")
LLM_DEFAULTS = {"fit_score": 0.5, "tech_overlap": 0.5, "stretch": 0.5}

_llm_client = None
_llm_limiter: TokenBucket | None = None

_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "summaries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "about": {"type": "string"},
                    "tldr": {"type": "string"},
                    "level": {"type": "string"},
                    "skills": {"type": "string"},
                    "fit": {"type": "string"},
                    # Numeric judgements for the hybrid re-ranker features (no
                    # bounds — Anthropic rejects them; we clamp [0,1] after parsing).
                    "fit_score": {"type": "number"},
                    "tech_overlap": {"type": "number"},
                    "stretch": {"type": "number"},
                },
                "required": ["id", "about", "tldr", "level", "skills", "fit",
                             "fit_score", "tech_overlap", "stretch"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["summaries"],
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


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _cache_key(card: dict) -> str:
    return f"{card.get('source', '')}:{card.get('external_id', '')}"


def _get_cached(keys: list[str]) -> dict[str, dict]:
    if not keys:
        return {}
    with connect() as conn:
        rows = conn.execute(
            f"SELECT cache_key, summary_json FROM posting_summaries "
            f"WHERE cache_key IN ({','.join('?' * len(keys))})",
            keys,
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        try:
            out[r["cache_key"]] = json.loads(r["summary_json"])
        except (ValueError, TypeError):
            continue
    return out


def _save_cached(key: str, data: dict) -> None:
    from datetime import datetime, timezone

    payload = {f: data.get(f, "") for f in _FIELDS}
    for f in LLM_FEATURES:  # numeric; for the re-ranker features
        payload[f] = data.get(f)
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO posting_summaries (cache_key, summary_json, created_at) "
            "VALUES (?, ?, ?)",
            (key, json.dumps(payload), datetime.now(timezone.utc).isoformat()),
        )


def cached_llm_features(keys: list[str]) -> dict[str, dict[str, float]]:
    """Map of cache_key -> {feature: value} for the numeric LLM judgements the
    summarizer has produced. Only numeric values are included; the re-ranker
    defaults anything missing. A posting with no numeric features is omitted."""
    out: dict[str, dict[str, float]] = {}
    for key, data in _get_cached(keys).items():
        feats = {f: float(data[f]) for f in LLM_FEATURES
                 if isinstance(data.get(f), (int, float))}
        if feats:
            out[key] = feats
    return out


def cached_fit_scores(keys: list[str]) -> dict[str, float]:
    """Back-compat: just the fit_score per key (numeric only)."""
    return {k: v["fit_score"] for k, v in cached_llm_features(keys).items()
            if "fit_score" in v}


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------

def summarize_batch(cards: list[dict], profile_block: str) -> dict[int, dict]:
    """One Haiku call for all ``cards``. Returns {index: {tldr, fit}}. Raises on
    failure (caller fails open)."""
    client, limiter = _get_llm()
    if not limiter.allow():
        raise RuntimeError("llm rate limited")
    listing = "\n".join(
        f"[{i}] {c.get('title', '')} @ {c.get('company', '')} — "
        f"{(c.get('description') or '')[:_DESC_CHARS]}"
        for i, c in enumerate(cards)
    )
    system = (
        "You write tight, honest job-card summaries that help a candidate decide in "
        "seconds. Read each posting and, grounded ONLY in what it says, return for "
        "every id:\n"
        "- about: <=18 words on what the COMPANY does/builds, plain language "
        "(e.g. 'Builds generative-media AI infrastructure and model APIs for "
        "developers'). Skip the hype.\n"
        "- tldr: <=22 words on what this person would actually DO day to day in "
        "THIS role. Concrete verbs, no buzzwords.\n"
        "- level: the experience the role targets, very short — e.g. 'Entry / "
        "new-grad', 'Internship', '1-2 yrs', 'Mid 3-5 yrs', 'Senior 5+ yrs'. Use "
        "'Not stated' only if truly absent.\n"
        "- skills: the 3-5 most important concrete skills/tech the posting names, "
        "comma-separated (e.g. 'Python, SQL, AWS'). 'Not stated' if none.\n"
        "- fit: <=12 words, honest read for THIS candidate and why (e.g. 'Strong "
        "entry fit — Python + ML' or 'Needs 5+ yrs you lack').\n"
        "- fit_score: a number 0.0-1.0 for how well THIS candidate fits and could "
        "land the role (1.0 = excellent + clearly qualified, 0.0 = wrong field or "
        "far over their level). Calibrate honestly across the batch.\n"
        "- tech_overlap: 0.0-1.0, the share of the role's key skills/tech the "
        "candidate clearly has (1.0 = has all of them, 0.0 = none).\n"
        "- stretch: 0.0-1.0, how much of a reach this is for them (0.0 = clearly "
        "qualified/easy, 1.0 = a big stretch beyond their experience).\n"
        "Be specific to each posting; never generic or copy-pasted across roles."
    )
    user = f"CANDIDATE:\n{profile_block}\n\nJOBS:\n{listing}"
    resp = client.messages.create(
        model=get_settings().anthropic_model,
        # ~110 tokens per role (5 fields + JSON), generous ceiling — too low here
        # truncates the JSON for a full deck and the whole batch fails to parse.
        max_tokens=min(4096, 160 + 120 * len(cards)),
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": _SUMMARY_SCHEMA}},
    )
    payload = next(b.text for b in resp.content if b.type == "text")
    out: dict[int, dict] = {}
    for item in json.loads(payload).get("summaries", []):
        try:
            res = {f: str(item.get(f, "")).strip() for f in _FIELDS}
        except (KeyError, TypeError, ValueError):
            continue
        for f in LLM_FEATURES:
            try:
                res[f] = max(0.0, min(1.0, float(item.get(f))))
            except (TypeError, ValueError):
                res[f] = None
        out[int(item["id"])] = res
    return out


def _apply(card: dict, data: dict) -> None:
    for f in _FIELDS:
        val = (data.get(f) or "").strip()
        # Drop unhelpful "Not stated" so the UI just hides that chip.
        if val and val.lower() != "not stated":
            card[f] = val


def enrich(cards: list[dict], profile_block: str, *, summarize=None) -> list[dict]:
    """Attach tldr/level/skills/fit to each card, using the cache first and ONE
    batched LLM call for the rest. Inactive/over-cap/error → cards returned
    unchanged; never raises. ``summarize`` injects the summarizer in tests."""
    if not cards:
        return cards
    s = get_settings()
    active = summarize is not None or (s.deck_tldr_enabled and s.use_llm_router)
    if not active:
        return cards

    cached = _get_cached([_cache_key(c) for c in cards])
    misses = [(i, c) for i, c in enumerate(cards) if _cache_key(c) not in cached]

    # Apply cache hits first.
    for c in cards:
        hit = cached.get(_cache_key(c))
        if hit:
            _apply(c, hit)

    if not misses:
        return cards

    if summarize is None:
        from . import jobstore

        if not jobstore.allow_summary_call():
            logger.info("deck TLDR daily cap reached — serving cached only")
            return cards
    try:
        judge = summarize or summarize_batch
        results = judge([c for _, c in misses], profile_block)
    except Exception:  # noqa: BLE001 — never block the deck on the summarizer
        logger.warning("deck TLDR failed; serving without summaries", exc_info=True)
        return cards
    if summarize is None:
        from . import jobstore

        jobstore.record_summary_call()

    for local_idx, (_, card) in enumerate(misses):
        res = results.get(local_idx)
        if not res:
            continue
        _apply(card, res)
        _save_cached(_cache_key(card), res)
    return cards
