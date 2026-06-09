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
# Fields the model returns and the UI renders, in order.
_FIELDS = ("tldr", "level", "skills", "fit")

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
                    "tldr": {"type": "string"},
                    "level": {"type": "string"},
                    "skills": {"type": "string"},
                    "fit": {"type": "string"},
                },
                "required": ["id", "tldr", "level", "skills", "fit"],
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
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO posting_summaries (cache_key, summary_json, created_at) "
            "VALUES (?, ?, ?)",
            (key, json.dumps(payload), datetime.now(timezone.utc).isoformat()),
        )


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
        "- tldr: <=20 words, plain spoken, what this person would actually DO day to "
        "day. Concrete verbs, no buzzwords, no company marketing.\n"
        "- level: the experience the role targets, very short — e.g. 'Entry / "
        "new-grad', 'Internship', '1-2 yrs', 'Mid 3-5 yrs', 'Senior 5+ yrs'. Use "
        "'Not stated' only if truly absent.\n"
        "- skills: the 3-5 most important concrete skills/tech the posting names, "
        "comma-separated (e.g. 'Python, SQL, AWS'). 'Not stated' if none.\n"
        "- fit: <=12 words, honest read for THIS candidate and why (e.g. 'Strong "
        "entry fit — Python + ML' or 'Needs 5+ yrs you lack').\n"
        "Be specific to each posting; never generic or copy-pasted across roles."
    )
    user = f"CANDIDATE:\n{profile_block}\n\nJOBS:\n{listing}"
    resp = client.messages.create(
        model=get_settings().anthropic_model,
        max_tokens=min(2400, 80 + 75 * len(cards)),
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": _SUMMARY_SCHEMA}},
    )
    payload = next(b.text for b in resp.content if b.type == "text")
    out: dict[int, dict] = {}
    for item in json.loads(payload).get("summaries", []):
        try:
            out[int(item["id"])] = {f: str(item.get(f, "")).strip() for f in _FIELDS}
        except (KeyError, TypeError, ValueError):
            continue
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
