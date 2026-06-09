"""Semantic embeddings for relevance matching (Matching v2, Phase 1).

Off by default. When ``EMBEDDING_ENABLED=true`` and a ``VOYAGE_API_KEY`` is set,
``matcher.score`` ranks postings by cosine similarity between the candidate
profile and each job description — semantic match, not keyword overlap.

Mirrors the cost-control rules used everywhere else here:

  * ``embed`` NEVER raises — on no-key / disabled / over-budget / rate-limit /
    any API error it returns ``[None, ...]`` so scoring degrades to the free
    keyword heuristic. The heuristic stays the CI path (tests never hit Voyage).
  * Daily budget cap + per-minute token bucket, same shape as the SerpApi
    aggregator (``jobstore.allow_embedding_call`` / ``record_embedding_call``).

Vectors are plain ``float32`` arrays — no numpy dependency (keeps the 512MB box
light). Cosine is computed in pure Python; at our scale (tens of postings per
tick) that's plenty fast.
"""
from __future__ import annotations

import logging
import math
from array import array

from .config import get_settings
from .ratelimit import TokenBucket

logger = logging.getLogger("embeddings")

_VOYAGE_URL = "https://api.voyageai.com/v1/embeddings"
# Per-request input cap (Voyage allows 128); we stay well under per tick anyway.
_MAX_BATCH = 128

_limiter: TokenBucket | None = None


def reset_for_tests() -> None:
    """Drop the rate-limiter singleton so env changes take effect between tests."""
    global _limiter
    _limiter = None


def _get_limiter() -> TokenBucket:
    global _limiter
    if _limiter is None:
        _limiter = TokenBucket(get_settings().embedding_rate_limit_per_min)
    return _limiter


# ---------------------------------------------------------------------------
# Vector (de)serialization — float32 BLOBs in SQLite
# ---------------------------------------------------------------------------

def to_blob(vec: list[float] | None) -> bytes | None:
    """Pack a vector into a compact float32 BLOB for ``job_postings.embedding``."""
    if not vec:
        return None
    return array("f", vec).tobytes()


def from_blob(blob: bytes | None) -> list[float] | None:
    """Unpack a float32 BLOB back into a list. None/empty -> None."""
    if not blob:
        return None
    a = array("f")
    a.frombytes(blob)
    return list(a)


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity in [-1, 1]; 0.0 when either vector is missing/degenerate."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# Embedding backend (Voyage)
# ---------------------------------------------------------------------------

def embed(texts: list[str], *, input_type: str = "document") -> list[list[float] | None]:
    """Embed ``texts`` via Voyage. Returns a list aligned with the input.

    Never raises: returns ``[None, ...]`` when embeddings are inactive, the daily
    budget is spent, the rate limit trips, or the API errors — so the caller can
    fall back to the free heuristic. ``input_type`` is "query" for the candidate
    profile and "document" for postings (Voyage tunes asymmetric retrieval).
    """
    n = len(texts)
    if n == 0:
        return []
    s = get_settings()
    if not s.embedding_active:
        return [None] * n

    from . import jobstore  # lazy: avoids an import cycle at module load

    if not jobstore.allow_embedding_call():
        logger.info("embedding daily cap reached — falling back to heuristic")
        return [None] * n
    if not _get_limiter().allow():
        logger.info("embedding rate limited — falling back to heuristic")
        return [None] * n

    import httpx

    try:
        resp = httpx.post(
            _VOYAGE_URL,
            headers={"Authorization": f"Bearer {s.voyage_api_key.strip()}"},
            json={
                "input": [t or " " for t in texts[:_MAX_BATCH]],
                "model": s.embedding_model,
                "input_type": input_type,
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception:  # noqa: BLE001 — never block discovery on the embedder
        logger.warning("embedding request failed; using heuristic", exc_info=True)
        return [None] * n

    jobstore.record_embedding_call()
    return _parse(data, n)


def _parse(data: dict, n: int) -> list[list[float] | None]:
    """Map Voyage's ``data[].embedding`` back to input order. Robust to gaps."""
    out: list[list[float] | None] = [None] * n
    if not isinstance(data, dict):
        return out
    for i, item in enumerate(data.get("data") or []):
        if not isinstance(item, dict):
            continue
        idx = item.get("index", i)
        vec = item.get("embedding")
        if isinstance(idx, int) and 0 <= idx < n and isinstance(vec, list):
            out[idx] = [float(x) for x in vec]
    return out
