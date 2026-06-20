"""A/B experiment: do the hybrid LLM judgement features improve the re-ranker?

Compares the re-ranker WITH vs WITHOUT the LLM features (insights.LLM_FEATURES:
fit_score, tech_overlap, stretch — the LLM's 0-1 reads, cached per posting) on
5-fold cross-validated AUC, and reports each feature's standalone AUC + weight.
Run the backfill first so labeled postings have the features:
    python -m scripts.backfill_fit_scores
    python -m scripts.llm_feature_experiment [user_id]
"""
from __future__ import annotations

import sys

from app import profile
from app.insights import LLM_DEFAULTS, LLM_FEATURES
from app.reranker import (
    FEATURES, Featurizer, _build_dataset, _fit, _labeled_examples, _llm_feats_for,
)
from scripts.analyze_reranker import _auc, _kfold_eval


def main(user: str = "local") -> None:
    prof = profile.get_profile(user)
    ex = _labeled_examples(user)
    if not ex:
        print(f"No labels for '{user}'."); return

    feats = _llm_feats_for(ex)
    featzr = Featurizer(prof, feats)
    X_full, y, w, n_pos, n_neg = _build_dataset(ex, featzr)
    n_llm = len(LLM_FEATURES)
    base_len = len(FEATURES) - n_llm
    X_base = [row[:base_len] for row in X_full]

    covered = sum(1 for e in ex if f"{e[3] or ''}:{e[4] or ''}" in feats)
    print(f"=== LLM-features A/B — user '{user}', {len(ex)} labels "
          f"({n_pos} apply / {n_pos and n_neg}) ===")
    print(f"coverage: {covered}/{len(ex)} postings have LLM features "
          f"(adding: {', '.join(LLM_FEATURES)})\n")

    acc_b, auc_b = _kfold_eval(X_base, y, w)
    acc_a, auc_a = _kfold_eval(X_full, y, w)
    print("--- 5-fold cross-validation ---")
    print(f"  WITHOUT LLM features ({base_len} features):  acc {acc_b:.1%}   AUC {auc_b:.3f}")
    print(f"  WITH    LLM features ({len(FEATURES)} features):  acc {acc_a:.1%}   AUC {auc_a:.3f}")
    d = auc_a - auc_b
    print(f"  => LLM features change AUC by {d:+.3f} "
          f"({'helps' if d > 0.01 else 'no real lift' if abs(d) <= 0.01 else 'hurts'})\n")

    print("--- each LLM feature, standalone AUC (vs swipe) ---")
    for i, f in enumerate(LLM_FEATURES):
        col = [row[base_len + i] for row in X_full]
        cov = [(c, yy) for c, yy in zip(col, y) if c != LLM_DEFAULTS[f]]
        a_all = _auc(y, col)
        a_cov = _auc([yy for _, yy in cov], [c for c, _ in cov]) if cov else float("nan")
        print(f"  {f:13} AUC {a_all:.3f} (all)   {a_cov:.3f} (covered, n={len(cov)})")

    wts, b = _fit(X_full, y, w)
    print("\n--- learned weights (|weight| desc) ---")
    for name, wt in sorted(zip(FEATURES, wts), key=lambda t: -abs(t[1])):
        print(f"  {name:13} {wt:+.2f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "local")
