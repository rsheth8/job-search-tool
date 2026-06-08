"""Personalized re-ranker (Matching v2, Phase 2).

The matcher gives a *generic* relevance score. This layer learns what **you**
actually act on — re-scoring postings with a tiny logistic-regression model
trained on your own labels:

    applied   -> positive (1)
    dismissed -> negative (0)
    snoozed   -> weak negative (0, half weight)  # "not now", milder than dismiss

Features per posting (all free, no API — computed from the stored row + profile):

    1. relevance      the matcher's own score (embedding or heuristic), 0..1
    2. kw_overlap     fraction of profile concepts present in the posting
    3. title_hit      a profile term lands in the *title* (strong intent signal)
    4. loc_match      a preferred location appears
    5. is_remote      the posting is remote
    6. first_party    from a company ATS (vs aggregator/RSS)

Design rules carried from the rest of the system:

  * **Cold-start safe.** Below the per-class label minimums (or with one class
    only) there's no model and ``rerank`` is a no-op — the matcher's order
    stands. The model only engages once it has enough of your signal.
  * **Pure-Python.** Logistic regression by gradient descent with L2; no numpy /
    scikit-learn (keeps the 512MB box light, mirrors the pure-Python cosine).
  * **Never blocks discovery.** Any training/scoring error degrades to the
    matcher's order.

Models persist as JSON in ``reranker_models`` and retrain in-place as labels
accumulate (``maybe_retrain``, called once per tick — cheap at this data size).
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
from datetime import datetime, timezone

from . import matcher
from .config import get_settings
from .db import connect
from .jobsources import JobPosting
from .jobsources import quality

logger = logging.getLogger("reranker")

FEATURES = ("relevance", "kw_overlap", "title_hit", "loc_match", "is_remote", "first_party")
_MODEL_VERSION = 1
_SNOOZE_WEIGHT = 0.5  # snoozed = mild negative, counts half as much as a dismiss

# Training hyperparameters — fixed, small data so these are uncritical.
_LR = 0.3
_EPOCHS = 600
_L2 = 0.01


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

class Featurizer:
    """Builds a feature vector from a posting against one profile.

    Reuses the matcher's term/location logic so 'relevant' means the same thing
    in both layers. Profile-derived sets are computed once per instance.
    """

    def __init__(self, profile: sqlite3.Row | None) -> None:
        self.terms = matcher._terms(profile)
        self.match_terms = matcher._match_terms(profile)
        self.locations = matcher._locations(profile)

    def features(
        self,
        *,
        title: str | None,
        location: str | None,
        description: str | None,
        source: str | None,
        relevance: float | None,
    ) -> list[float]:
        title_l = (title or "").lower()
        haystack = f"{title or ''} {location or ''} {description or ''}".lower()
        kw_overlap = (
            sum(1 for t in self.terms if t in haystack) / len(self.terms)
            if self.terms else 0.0
        )
        title_hit = (
            1.0 if any(matcher._term_in(t, title_l) for t in self.match_terms) else 0.0
        )
        loc_match = (
            1.0 if self.locations and any(loc in haystack for loc in self.locations)
            else 0.0
        )
        is_remote = 1.0 if "remote" in haystack else 0.0
        first_party = 1.0 if (source or "").lower() in quality.FIRST_PARTY_SOURCES else 0.0
        return [
            float(relevance if relevance is not None else 0.5),
            kw_overlap,
            title_hit,
            loc_match,
            is_remote,
            first_party,
        ]


# ---------------------------------------------------------------------------
# Pure-Python logistic regression
# ---------------------------------------------------------------------------

def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)  # avoids overflow for very negative z
    return ez / (1.0 + ez)


def _fit(
    X: list[list[float]], y: list[float], w: list[float]
) -> tuple[list[float], float]:
    """Weighted L2-regularized logistic regression via gradient descent."""
    n_feat = len(X[0])
    weights = [0.0] * n_feat
    bias = 0.0
    wsum = sum(w) or 1.0
    for _ in range(_EPOCHS):
        grad_w = [0.0] * n_feat
        grad_b = 0.0
        for xi, yi, wi in zip(X, y, w):
            pred = _sigmoid(sum(wj * xj for wj, xj in zip(weights, xi)) + bias)
            err = (pred - yi) * wi
            for j in range(n_feat):
                grad_w[j] += err * xi[j]
            grad_b += err
        for j in range(n_feat):
            grad_w[j] = grad_w[j] / wsum + _L2 * weights[j]
            weights[j] -= _LR * grad_w[j]
        bias -= _LR * (grad_b / wsum)
    return weights, bias


def _predict(weights: list[float], bias: float, x: list[float]) -> float:
    return _sigmoid(sum(wj * xj for wj, xj in zip(weights, x)) + bias)


# ---------------------------------------------------------------------------
# Labels + training
# ---------------------------------------------------------------------------

def _labeled_rows(user_id: str) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT title, location, description, source, relevance_score, status "
            "FROM job_postings WHERE user_id = ? "
            "AND status IN ('applied', 'dismissed', 'snoozed')",
            (user_id,),
        ).fetchall()


def _build_dataset(
    rows: list[sqlite3.Row], feat: Featurizer
) -> tuple[list[list[float]], list[float], list[float], int, int]:
    """Returns (X, y, sample_weight, n_positive, n_negative)."""
    X: list[list[float]] = []
    y: list[float] = []
    w: list[float] = []
    n_pos = n_neg = 0
    for r in rows:
        x = feat.features(
            title=r["title"], location=r["location"], description=r["description"],
            source=r["source"], relevance=r["relevance_score"],
        )
        if r["status"] == "applied":
            label, weight = 1.0, 1.0
            n_pos += 1
        else:  # dismissed | snoozed
            label = 0.0
            weight = _SNOOZE_WEIGHT if r["status"] == "snoozed" else 1.0
            n_neg += 1
        X.append(x)
        y.append(label)
        w.append(weight)
    return X, y, w, n_pos, n_neg


def train(user_id: str, profile: sqlite3.Row | None) -> dict | None:
    """Train + persist a model from the user's labels. Returns the model dict, or
    None when there isn't enough signal yet (cold start)."""
    s = get_settings()
    rows = _labeled_rows(user_id)
    feat = Featurizer(profile)
    X, y, w, n_pos, n_neg = _build_dataset(rows, feat)
    if n_pos < s.reranker_min_positive or n_neg < s.reranker_min_negative:
        return None
    try:
        weights, bias = _fit(X, y, w)
    except (ValueError, OverflowError):
        logger.warning("reranker fit failed for %s", user_id, exc_info=True)
        return None
    model = {
        "version": _MODEL_VERSION,
        "features": list(FEATURES),
        "weights": weights,
        "bias": bias,
        "n_labels": len(rows),
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_model(user_id, model)
    logger.info(
        "reranker trained for %s (%d pos / %d neg)", user_id, n_pos, n_neg
    )
    return model


def maybe_retrain(user_id: str, profile: sqlite3.Row | None) -> None:
    """Train on first eligibility and whenever the label count changed. Cheap to
    call every tick (tiny data); never raises."""
    try:
        current = _label_count(user_id)
        existing = load_model(user_id)
        if existing is not None and existing.get("n_labels") == current:
            return  # nothing new to learn from
        train(user_id, profile)
    except Exception:  # noqa: BLE001 — re-ranking is best-effort
        logger.warning("reranker maybe_retrain failed for %s", user_id, exc_info=True)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def rerank(
    user_id: str,
    profile: sqlite3.Row | None,
    scored: list[tuple[JobPosting, float]],
) -> list[tuple[JobPosting, float]]:
    """Re-score with the user's model, best first. No model → input unchanged
    (cold start). Never raises — falls back to the given order on any error."""
    if not scored:
        return scored
    model = load_model(user_id)
    if model is None:
        return scored
    try:
        weights, bias = model["weights"], model["bias"]
        feat = Featurizer(profile)
        out = []
        for posting, base in scored:
            x = feat.features(
                title=posting.title, location=posting.location,
                description=posting.description, source=posting.source,
                relevance=base,
            )
            out.append((posting, round(_predict(weights, bias, x), 3)))
        out.sort(key=lambda t: t[1], reverse=True)
        return out
    except Exception:  # noqa: BLE001
        logger.warning("reranker scoring failed for %s", user_id, exc_info=True)
        return scored


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _label_count(user_id: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM job_postings WHERE user_id = ? "
            "AND status IN ('applied', 'dismissed', 'snoozed')",
            (user_id,),
        ).fetchone()[0]


def _save_model(user_id: str, model: dict) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO reranker_models (user_id, model_json, n_labels, trained_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                model_json = excluded.model_json,
                n_labels   = excluded.n_labels,
                trained_at = excluded.trained_at
            """,
            (user_id, json.dumps(model), model["n_labels"], model["trained_at"]),
        )


def load_model(user_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT model_json FROM reranker_models WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        model = json.loads(row["model_json"])
    except (ValueError, TypeError):
        return None
    if model.get("version") != _MODEL_VERSION or model.get("features") != list(FEATURES):
        return None  # schema drift — ignore until retrained
    return model
