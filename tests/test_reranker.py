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


def test_outcome_weighting_grades_applied_labels_by_stage():
    """An 'applied' posting whose CRM application progressed to an Onsite is a
    stronger positive than a bare 'Applied'; a Rejected one is weaker."""
    from app import store

    _seed_label("u1", "applied", ext="1", title="Software Engineer")
    _seed_label("u1", "applied", ext="2", title="Data Scientist", desc="Models.")
    _seed_label("u1", "applied", ext="3", title="ML Engineer", desc="Pipelines.")
    # Matching CRM applications carry the real outcome stage.
    store.create_application("u1", "Acme", "Software Engineer", status="Onsite")
    store.create_application("u1", "Acme", "Data Scientist", status="Rejected")
    # ext=3 has no logged application -> baseline.

    grade = reranker._outcome_grader("u1")
    assert grade("Acme", "Software Engineer") == reranker._OUTCOME_WEIGHTS["Onsite"]
    assert grade("Acme", "Data Scientist") == reranker._OUTCOME_WEIGHTS["Rejected"]
    assert grade("Acme", "ML Engineer") == reranker._DEFAULT_APPLIED_WEIGHT

    # And it flows through into the training weights.
    rows = reranker._labeled_examples("u1")
    by_title = {r[0]: r[7] for r in rows}  # title -> weight
    assert by_title["Software Engineer"] == reranker._OUTCOME_WEIGHTS["Onsite"]
    assert by_title["Data Scientist"] == reranker._OUTCOME_WEIGHTS["Rejected"]
    assert by_title["ML Engineer"] == reranker._DEFAULT_APPLIED_WEIGHT


def test_outcome_weighting_disabled_keeps_baseline(monkeypatch):
    from app import config, store

    _seed_label("u1", "applied", ext="1", title="Software Engineer")
    store.create_application("u1", "Acme", "Software Engineer", status="Offer")
    monkeypatch.setattr(config.get_settings(), "reranker_outcome_weighting", False)
    grade = reranker._outcome_grader("u1")
    assert grade("Acme", "Software Engineer") == reranker._DEFAULT_APPLIED_WEIGHT


def test_auc_ranks_separable_scores():
    perfect = [(0.9, 1.0), (0.8, 1.0), (0.2, 0.0), (0.1, 0.0)]
    assert reranker._auc(perfect) == 1.0
    inverted = [(0.1, 1.0), (0.2, 1.0), (0.8, 0.0), (0.9, 0.0)]
    assert reranker._auc(inverted) == 0.0
    assert reranker._auc([(0.5, 1.0)]) == 0.5  # one class only -> uninformative


def _wide(*vals) -> list[float]:
    """A full-width feature vector with the given leading values, rest zero."""
    x = [0.0] * len(reranker.FEATURES)
    for i, v in enumerate(vals):
        x[i] = v
    return x


def _incumbent(*weights) -> dict:
    w = [0.0] * len(reranker.FEATURES)
    for i, v in enumerate(weights):
        w[i] = v
    return {"version": reranker._MODEL_VERSION, "features": list(reranker.FEATURES),
            "weights": w, "bias": 0.0}


def test_beats_incumbent_promotes_on_schema_or_thin_data():
    # Schema drift -> always promote.
    assert reranker._beats_incumbent([_wide(1.0)], [1.0], [1.0],
                                     {"features": ["only_one"], "weights": [1.0], "bias": 0.0})
    # Too few labels to carve a balanced hold-out -> promote.
    X = [_wide(1.0), _wide(0.0)]
    assert reranker._beats_incumbent(X, [1.0, 0.0], [1.0, 1.0], _incumbent(5.0))


def test_beats_incumbent_keeps_better_incumbent():
    """A candidate that overfits a spurious train-only feature and ranks the
    held-out rows worse than the incumbent is rejected."""
    # feature0 is the true signal (separates hold-out); feature1 separates the
    # TRAIN rows but is reversed on the hold-out, so a candidate that leans on it
    # generalizes worse than the feature0-only incumbent.
    holdout_pos, holdout_neg = {0, 5}, {10, 15}
    X, y = [], []
    for i in range(10):   # positives
        f1 = 0.0 if i in holdout_pos else 1.0
        X.append(_wide(1.0, f1)); y.append(1.0)
    for i in range(10, 20):  # negatives
        f1 = 1.0 if i in holdout_neg else 0.0
        X.append(_wide(0.0, f1)); y.append(0.0)
    w = [1.0] * len(X)
    incumbent = _incumbent(5.0, 0.0)  # trusts only the true signal
    assert reranker._beats_incumbent(X, y, w, incumbent) is False


def test_beats_incumbent_promotes_clearly_better_candidate():
    """An inverted incumbent (ranks backwards) is beaten by a fresh, correct fit."""
    X = [_wide(1.0) for _ in range(8)] + [_wide(0.0) for _ in range(8)]
    y = [1.0] * 8 + [0.0] * 8
    w = [1.0] * 16
    inverted = _incumbent(-5.0)  # ranks positives below negatives
    assert reranker._beats_incumbent(X, y, w, inverted) is True


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
