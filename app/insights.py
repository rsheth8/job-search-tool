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

_DESC_CHARS = 320  # JD slice sent per role — enough to summarize, few tokens

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
                    "fit": {"type": "string"},
                },
                "required": ["id", "tldr", "fit"],
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
            f"SELECT cache_key, tldr, fit FROM posting_summaries "
            f"WHERE cache_key IN ({','.join('?' * len(keys))})",
            keys,
        ).fetchall()
    return {r["cache_key"]: {"tldr": r["tldr"], "fit": r["fit"]} for r in rows}


def _save_cached(key: str, tldr: str, fit: str) -> None:
    from datetime import datetime, timezone

    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO posting_summaries (cache_key, tldr, fit, created_at) "
            "VALUES (?, ?, ?, ?)",
            (key, tldr, fit, datetime.now(timezone.utc).isoformat()),
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
        "Summarize each job for a candidate in plain, simple language. For each id "
        "return: 'tldr' — at most 22 words on what the role actually does day to "
        "day (no buzzwords); and 'fit' — at most 12 words on whether it suits this "
        "candidate and why (e.g. 'Good entry-level fit' or 'Wants ML research "
        "experience'). Be direct and honest."
    )
    user = f"CANDIDATE:\n{profile_block}\n\nJOBS:\n{listing}"
    resp = client.messages.create(
        model=get_settings().anthropic_model,
        max_tokens=min(1800, 60 + 55 * len(cards)),
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"format": {"type": "json_schema", "schema": _SUMMARY_SCHEMA}},
    )
    payload = next(b.text for b in resp.content if b.type == "text")
    out: dict[int, dict] = {}
    for item in json.loads(payload).get("summaries", []):
        try:
            out[int(item["id"])] = {
                "tldr": str(item.get("tldr", "")).strip(),
                "fit": str(item.get("fit", "")).strip(),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return out


def enrich(cards: list[dict], profile_block: str, *, summarize=None) -> list[dict]:
    """Attach 'tldr' and 'fit' to each card, using the cache first and ONE batched
    LLM call for the rest. Inactive/over-cap/error → cards returned unchanged (no
    tldr); never raises. ``summarize`` injects the summarizer in tests."""
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
            c["tldr"], c["fit"] = hit["tldr"], hit["fit"]

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
        card["tldr"], card["fit"] = res["tldr"], res["fit"]
        _save_cached(_cache_key(card), res["tldr"], res["fit"])
    return cards
