"""Personalized re-ranker (Matching v2, Phase 2).

The matcher gives a *generic* relevance score. This layer learns what **you**
actually act on — re-scoring postings with a tiny logistic-regression model
trained on your own labels:

    applied   -> positive (1)
    dismissed -> negative (0)
    snoozed   -> weak negative (0, half weight)  # "not now", milder than dismiss

Features per posting (all free, no API — computed from the stored row + profile):

    1. relevance      the matcher's own score (LLM or heuristic), 0..1
    2. kw_overlap     fraction of profile concepts present in the posting
    3. title_hit      a profile term lands in the *title* (strong intent signal)
    4. loc_match      a preferred location appears
    5. is_remote      the posting is remote
    6. first_party    from a company ATS (vs RSS/directory)
    7. freshness      1.0 if posted in the last 48h, decaying to 0 by ~45 days
    8. fillable       Greenhouse / Lever / Ashby apply URL the iOS engine can drive


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
from . import posting_match
from . import store
from . import ats
from . import shortlist
from .config import get_settings
from .db import connect
from .jobsources import JobPosting
from .jobsources import quality

logger = logging.getLogger("reranker")

# Free features only (no LLM judgement features). Feature schema bump invalidates
# older persisted models so they retrain on the new vector.
FEATURES = ("relevance", "kw_overlap", "title_hit", "loc_match", "is_remote",
            "first_party", "freshness", "fillable")
_MODEL_VERSION = 5  # freshness + fillable
_SNOOZE_WEIGHT = 0.5  # snoozed = mild negative, counts half as much as a dismiss

# Graded positive weights by the application's real outcome stage (the CRM
# funnel). All remain positive labels (you wanted them) — the weight rewards
# traction: a role that reached an interview/offer teaches the model more than a
# bare 'Applied', and one that was rejected/ghosted teaches it less. A plain
# 'Applied' (or no matching CRM application) keeps the baseline 1.0, so this is
# pure refinement — it never changes a label, only how much each one counts.
_OUTCOME_WEIGHTS = {
    "Applied": 1.0,
    "Phone screen": 2.0,
    "Interview": 2.0,
    "Onsite": 2.5,
    "Offer": 3.0,
    "Rejected": 0.75,   # applied but didn't progress — still a weak positive
    "Ghosted": 0.75,
}
_DEFAULT_APPLIED_WEIGHT = 1.0

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
        external_id: str | None = None,
        posted_at: str | None = None,
        url: str | None = None,
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
        first_party = 1.0 if (source or "").lower() in quality.PREFERRED_APPLY_SOURCES else 0.0
        return [
            float(relevance if relevance is not None else 0.5),
            kw_overlap,
            title_hit,
            loc_match,
            is_remote,
            first_party,
            shortlist.freshness_score(posted_at),
            1.0 if ats.is_fillable_form(url) else 0.0,
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

def _outcome_grader(user_id: str):
    """Return ``f(company, title) -> weight`` for a positive ('applied') label,
    graded by the furthest stage of the matching CRM application. Disabled, or no
    matching application, yields the baseline weight. The match reuses
    ``posting_match`` (same company+title logic the apply flow links them with)."""
    if not get_settings().reranker_outcome_weighting:
        return lambda company, title: _DEFAULT_APPLIED_WEIGHT
    apps = store.application_outcomes(user_id)
    if not apps:
        return lambda company, title: _DEFAULT_APPLIED_WEIGHT

    def grade(company: str | None, title: str | None) -> float:
        best: float | None = None
        for comp, role, stages in apps:
            if posting_match.matches_application(comp, role, company, title):
                # Credit the furthest stage this application reached.
                wt = max((_OUTCOME_WEIGHTS.get(s, _DEFAULT_APPLIED_WEIGHT) for s in stages),
                         default=_DEFAULT_APPLIED_WEIGHT)
                if best is None or wt > best:  # best across duplicate applications
                    best = wt
        return best if best is not None else _DEFAULT_APPLIED_WEIGHT

    return grade


def _labeled_examples(user_id: str) -> list[tuple]:
    """Normalized (title, location, description, source, external_id, relevance,
    y, weight) training rows from BOTH real applications (job_postings status) and
    the swipe trainer labels (training_labels) — so the model learns from whichever
    signal exists.

        applied / swipe-'like'  -> y=1
        dismissed / swipe-'pass'-> y=0
        snoozed                 -> y=0 at half weight ('not now', milder)
    """
    grade = _outcome_grader(user_id)
    out: list[tuple] = []
    with connect() as conn:
        for r in conn.execute(
            "SELECT title, location, description, source, external_id, "
            "relevance_score, company, status, posted_at, url FROM job_postings "
            "WHERE user_id = ? AND status IN ('applied', 'dismissed', 'snoozed')",
            (user_id,),
        ):
            if r["status"] == "applied":
                y, w = 1.0, grade(r["company"], r["title"])
            else:
                y = 0.0
                w = _SNOOZE_WEIGHT if r["status"] == "snoozed" else 1.0
            out.append((r["title"], r["location"], r["description"], r["source"],
                        r["external_id"], r["relevance_score"], y, w,
                        r["posted_at"], r["url"]))
        for r in conn.execute(
            "SELECT title, location, description, source, external_id, "
            "relevance_score, label, url FROM training_labels WHERE user_id = ?",
            (user_id,),
        ):
            y = 1.0 if r["label"] == "like" else 0.0
            out.append((r["title"], r["location"], r["description"], r["source"],
                        r["external_id"], r["relevance_score"], y, 1.0,
                        "", r["url"]))
    return out


def _build_dataset(
    examples: list[tuple], feat: Featurizer
) -> tuple[list[list[float]], list[float], list[float], int, int]:
    """Returns (X, y, sample_weight, n_positive, n_negative)."""
    X: list[list[float]] = []
    y: list[float] = []
    w: list[float] = []
    n_pos = n_neg = 0
    for (title, location, description, source, external_id, relevance, label,
         weight, posted_at, url) in examples:
        X.append(feat.features(title=title, location=location,
                               description=description, source=source,
                               relevance=relevance, external_id=external_id,
                               posted_at=posted_at, url=url))
        y.append(label)
        w.append(weight)
        if label >= 0.5:
            n_pos += 1
        else:
            n_neg += 1
    return X, y, w, n_pos, n_neg


def _train_model(
    user_id: str, profile: sqlite3.Row | None
) -> tuple[dict, list[list[float]], list[float], list[float]] | None:
    """Fit a model from the user's labels WITHOUT persisting. Returns
    ``(model, X, y, sample_weight)``, or None below the per-class minimums (cold
    start). The dataset is handed back so callers can run a hold-out check before
    deciding to promote."""
    s = get_settings()
    examples = _labeled_examples(user_id)
    feat = Featurizer(profile)
    X, y, w, n_pos, n_neg = _build_dataset(examples, feat)
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
        "n_labels": len(examples),
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }
    return model, X, y, w


def train(user_id: str, profile: sqlite3.Row | None) -> dict | None:
    """Train + persist a model from the user's labels. Returns the model dict, or
    None when there isn't enough signal yet (cold start). This is the explicit
    path — it always persists; the hold-out guard lives in ``maybe_retrain``."""
    built = _train_model(user_id, profile)
    if built is None:
        return None
    model = built[0]
    _save_model(user_id, model)
    logger.info("reranker trained for %s (%d labels)", user_id, model["n_labels"])
    return model


def maybe_retrain(user_id: str, profile: sqlite3.Row | None) -> None:
    """Train on first eligibility and whenever the label count changed. Cheap to
    call every tick (tiny data); never raises.

    Hold-out guard: once an incumbent model exists, a freshly-fit candidate only
    replaces it if it ranks held-out labels at least as well (AUC). This stops a
    noisy batch of new labels from silently degrading production ranking."""
    try:
        current = _label_count(user_id)
        existing = load_model(user_id)
        if existing is not None and existing.get("n_labels") == current:
            return  # nothing new to learn from
        built = _train_model(user_id, profile)
        if built is None:
            return
        model, X, y, w = built
        if existing is not None and not _beats_incumbent(X, y, w, existing):
            logger.info(
                "reranker: candidate didn't beat incumbent for %s — keeping current",
                user_id,
            )
            return
        _save_model(user_id, model)
        logger.info("reranker trained for %s (%d labels)", user_id, model["n_labels"])
    except Exception:  # noqa: BLE001 — re-ranking is best-effort
        logger.warning("reranker maybe_retrain failed for %s", user_id, exc_info=True)


# ---------------------------------------------------------------------------
# Hold-out promotion guard
# ---------------------------------------------------------------------------

_HOLDOUT_FRAC = 0.25  # share of each class held out to score candidate vs incumbent


def _auc(pairs: list[tuple[float, float]]) -> float:
    """Rank-AUC (probability a random positive outscores a random negative).
    0.5 when a class is missing (uninformative)."""
    pos = [s for s, label in pairs if label >= 0.5]
    neg = [s for s, label in pairs if label < 0.5]
    if not pos or not neg:
        return 0.5
    wins = ties = 0
    for ps in pos:
        for ns in neg:
            if ps > ns:
                wins += 1
            elif ps == ns:
                ties += 1
    return (wins + 0.5 * ties) / (len(pos) * len(neg))


def _spread(idx: list[int], frac: float) -> list[int]:
    """Pick ``frac`` of ``idx`` evenly across its length (deterministic)."""
    n = int(len(idx) * frac)
    if n < 1:
        return []
    step = len(idx) / n
    return [idx[int(i * step)] for i in range(n)]


def _beats_incumbent(
    X: list[list[float]], y: list[float], w: list[float], incumbent: dict
) -> bool:
    """True if a model refit on a train split ranks the held-out labels at least
    as well as the incumbent. Falls back to True (promote) when there isn't enough
    data to carve a balanced hold-out — the freshest fit on more data wins by
    default. The incumbent is scored on rows it was trained on, so this check is
    deliberately conservative (biased toward keeping the current model)."""
    if incumbent.get("features") != list(FEATURES):
        return True  # schema differs — let the new model replace it
    pos = [i for i, v in enumerate(y) if v >= 0.5]
    neg = [i for i, v in enumerate(y) if v < 0.5]
    if int(len(pos) * _HOLDOUT_FRAC) < 1 or int(len(neg) * _HOLDOUT_FRAC) < 1:
        return True  # too few labels to hold out a balanced eval set
    # Spread the hold-out evenly across each class (not the tail) so it stays
    # representative regardless of label insertion order.
    holdout = set(_spread(pos, _HOLDOUT_FRAC)) | set(_spread(neg, _HOLDOUT_FRAC))
    tr = [i for i in range(len(y)) if i not in holdout]
    if not any(y[i] >= 0.5 for i in tr) or not any(y[i] < 0.5 for i in tr):
        return True  # train split lost a class — can't evaluate fairly
    try:
        cw, cb = _fit([X[i] for i in tr], [y[i] for i in tr], [w[i] for i in tr])
    except (ValueError, OverflowError):
        return True
    iw, ib = incumbent["weights"], incumbent["bias"]
    cand = [(_predict(cw, cb, X[i]), y[i]) for i in holdout]
    inc = [(_predict(iw, ib, X[i]), y[i]) for i in holdout]
    return _auc(cand) >= _auc(inc)


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------

def predict(
    user_id: str,
    profile: sqlite3.Row | None,
    scored: list[tuple[JobPosting, float]],
) -> list[tuple[JobPosting, float]] | None:
    """Per-posting model probability, in the input order (NOT sorted). Returns
    None when there's no trained model (cold start) or on any error — callers
    treat that as 'no signal'. Used by active-learning deck selection, which wants
    the raw probabilities (to find the most *uncertain* postings), not a ranking."""
    model = load_model(user_id)
    if model is None:
        return None
    try:
        weights, bias = model["weights"], model["bias"]
        feat = Featurizer(profile)
        return [
            (
                posting,
                _predict(
                    weights, bias,
                    feat.features(
                        title=posting.title, location=posting.location,
                        description=posting.description, source=posting.source,
                        relevance=base, external_id=posting.external_id,
                        posted_at=posting.posted_at, url=posting.url,
                    ),
                ),
            )
            for posting, base in scored
        ]
    except Exception:  # noqa: BLE001
        logger.warning("reranker predict failed for %s", user_id, exc_info=True)
        return None


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
                relevance=base, external_id=posting.external_id,
                posted_at=posting.posted_at, url=posting.url,
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
    """Total training examples (real applications + swipe labels) — drives the
    'have new labels appeared since last train?' check in maybe_retrain."""
    with connect() as conn:
        n_posts = conn.execute(
            "SELECT COUNT(*) FROM job_postings WHERE user_id = ? "
            "AND status IN ('applied', 'dismissed', 'snoozed')",
            (user_id,),
        ).fetchone()[0]
        n_swipes = conn.execute(
            "SELECT COUNT(*) FROM training_labels WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]
    return n_posts + n_swipes


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
