"""Phase 2 personalized re-ranker: featurizer, pure-Python LR, train/persist,
cold-start fallback, and the rerank ordering."""
from __future__ import annotations

import sqlite3

import pytest

from app import jobstore, reranker
from app.db import connect
from app.jobsources import JobPosting


def _profile(roles="software engineer", locations="remote", **extra) -> sqlite3.Row:
    cols = {"roles": roles, "keywords": "", "locations": locations, "seniority": "",
            "resume_summary": "", "min_relevance": None}
    cols.update(extra)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    keys = ", ".join(cols)
    conn.execute(f"CREATE TABLE p ({keys})")
    conn.execute(f"INSERT INTO p ({keys}) VALUES ({', '.join('?' * len(cols))})",
                 tuple(cols.values()))
    return conn.execute("SELECT * FROM p").fetchone()


def _posting(title="Software Engineer", desc="Build software.", source="greenhouse",
             location="Remote", ext="1") -> JobPosting:
    return JobPosting(source=source, external_id=ext, title=title, url="https://x",
                      company="Acme", location=location, description=desc)


def _seed_label(user_id, status, *, ext, title="Software Engineer",
                source="greenhouse", relevance=0.8, location="Remote", desc="Build software."):
    p = _posting(title=title, desc=desc, source=source, location=location, ext=ext)
    jobstore.save_posting(user_id, p, relevance_score=relevance, status=status)


# ---------------------------------------------------------------------------
# Featurizer
# ---------------------------------------------------------------------------

def test_featurizer_shape_and_values():
    feat = reranker.Featurizer(_profile(roles="software engineer", locations="remote"))
    x = feat.features(title="Software Engineer", location="Remote",
                      description="Build software.", source="greenhouse", relevance=0.7)
    assert len(x) == len(reranker.FEATURES)
    assert x[0] == pytest.approx(0.7)   # relevance passthrough
    assert x[2] == 1.0                  # title_hit ("software engineer" in title)
    assert x[4] == 1.0                  # is_remote
    assert x[5] == 1.0                  # first_party (greenhouse)


def test_featurizer_llm_features_from_cache_else_neutral():
    from app.insights import LLM_FEATURES
    base = len(reranker.FEATURES) - len(LLM_FEATURES)
    feat = reranker.Featurizer(_profile(), {
        "greenhouse:job1": {"fit_score": 0.9, "tech_overlap": 0.8, "stretch": 0.2}})
    hit = feat.features(title="X", location="", description="", source="greenhouse",
                        relevance=0.5, external_id="job1")
    miss = feat.features(title="X", location="", description="", source="greenhouse",
                         relevance=0.5, external_id="unknown")
    assert hit[base + LLM_FEATURES.index("fit_score")] == 0.9      # from cache
    assert hit[base + LLM_FEATURES.index("tech_overlap")] == 0.8
    assert miss[base:] == [0.5, 0.5, 0.5]                          # all neutral defaults


def test_featurizer_relevance_defaults_when_missing():
    feat = reranker.Featurizer(_profile())
    x = feat.features(title="X", location="", description="", source="rss", relevance=None)
    assert x[0] == pytest.approx(0.5)
    assert x[5] == 0.0  # rss is not first-party


# ---------------------------------------------------------------------------
# Pure-Python logistic regression learns a separable signal
# ---------------------------------------------------------------------------

def test_fit_learns_separable_pattern():
    # Feature 0 perfectly predicts the label.
    X = [[1.0, 0.0], [0.9, 1.0], [0.8, 0.0], [0.1, 1.0], [0.0, 0.0], [0.2, 1.0]]
    y = [1.0, 1.0, 1.0, 0.0, 0.0, 0.0]
    w = [1.0] * len(X)
    weights, bias = reranker._fit(X, y, w)
    assert reranker._predict(weights, bias, [1.0, 0.0]) > 0.6
    assert reranker._predict(weights, bias, [0.0, 0.0]) < 0.4


# ---------------------------------------------------------------------------
# train(): cold start + persistence
# ---------------------------------------------------------------------------

def test_train_returns_none_below_minimums():
    prof = _profile()
    _seed_label("u1", "applied", ext="1")
    _seed_label("u1", "dismissed", ext="2")
    assert reranker.train("u1", prof) is None  # not enough of either class
    assert reranker.load_model("u1") is None


def test_train_returns_none_with_one_class_only():
    prof = _profile()
    for i in range(6):
        _seed_label("u1", "applied", ext=str(i))
    assert reranker.train("u1", prof) is None  # no negatives


def test_train_persists_and_loads():
    prof = _profile()
    for i in range(5):
        _seed_label("u1", "applied", ext=f"p{i}", source="greenhouse", relevance=0.9)
    for i in range(5):
        _seed_label("u1", "dismissed", ext=f"n{i}", source="rss", relevance=0.2)
    model = reranker.train("u1", prof)
    assert model is not None
    assert model["features"] == list(reranker.FEATURES)
    assert model["n_labels"] == 10
    loaded = reranker.load_model("u1")
    assert loaded is not None
    assert loaded["weights"] == model["weights"]


def test_load_model_rejects_schema_drift():
    prof = _profile()
    for i in range(5):
        _seed_label("u1", "applied", ext=f"p{i}")
    for i in range(5):
        _seed_label("u1", "dismissed", ext=f"n{i}")
    reranker.train("u1", prof)
    # Corrupt the stored feature list -> load_model should reject it.
    with connect() as conn:
        conn.execute("UPDATE reranker_models SET model_json = ? WHERE user_id = 'u1'",
                     ('{"version": 1, "features": ["only_one"], "weights": [1], "bias": 0}',))
    assert reranker.load_model("u1") is None


# ---------------------------------------------------------------------------
# rerank(): cold-start no-op + learned reordering
# ---------------------------------------------------------------------------

def test_rerank_noop_without_model():
    prof = _profile()
    scored = [(_posting(ext="1"), 0.4), (_posting(ext="2"), 0.9)]
    assert reranker.rerank("u1", prof, scored) == scored


def test_rerank_reorders_by_learned_preference():
    prof = _profile(roles="software engineer", locations="remote")
    # Teach the model: first-party greenhouse roles = good; rss roles = bad,
    # regardless of the matcher's base score.
    for i in range(6):
        _seed_label("u1", "applied", ext=f"p{i}", source="greenhouse", relevance=0.5)
    for i in range(6):
        _seed_label("u1", "dismissed", ext=f"n{i}", source="rss", relevance=0.5)
    assert reranker.train("u1", prof) is not None

    gh = _posting(source="greenhouse", ext="new-gh")
    rss = _posting(source="rss", ext="new-rss")
    # Matcher ranked the rss one higher; the personal model should flip it.
    out = reranker.rerank("u1", prof, [(rss, 0.8), (gh, 0.6)])
    assert out[0][0].source == "greenhouse"


def test_maybe_retrain_trains_then_skips_when_unchanged():
    prof = _profile()
    for i in range(5):
        _seed_label("u1", "applied", ext=f"p{i}")
    for i in range(5):
        _seed_label("u1", "dismissed", ext=f"n{i}")
    reranker.maybe_retrain("u1", prof)
    first = reranker.load_model("u1")
    assert first is not None
    # No new labels -> no retrain -> same trained_at.
    reranker.maybe_retrain("u1", prof)
    assert reranker.load_model("u1")["trained_at"] == first["trained_at"]
