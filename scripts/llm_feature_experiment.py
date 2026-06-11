"""A/B experiment: does the hybrid LLM fit-score feature improve the re-ranker?

Compares the re-ranker WITH vs WITHOUT the ``llm_fit`` feature (the LLM's own 0-1
fit judgement, cached per posting) on 5-fold cross-validated AUC/accuracy. Run the
backfill first so the labeled postings have fit scores:
    python -m scripts.backfill_fit_scores
    python -m scripts.llm_feature_experiment [user_id]
"""
from __future__ import annotations

import sys

from app import profile
from app.reranker import (
    FEATURES, Featurizer, _build_dataset, _fit, _fit_scores_for, _labeled_examples,
)
from scripts.analyze_reranker import _auc, _kfold_eval


def main(user: str = "local") -> None:
    prof = profile.get_profile(user)
    ex = _labeled_examples(user)
    if not ex:
        print(f"No labels for '{user}'."); return

    fit_scores = _fit_scores_for(ex)
    feat = Featurizer(prof, fit_scores)
    X_full, y, w, n_pos, n_neg = _build_dataset(ex, feat)  # includes llm_fit (last col)
    X_base = [row[:-1] for row in X_full]                  # drop llm_fit
    llm_idx = FEATURES.index("llm_fit")
    sims = [row[llm_idx] for row in X_full]

    covered = sum(1 for s in sims if s != 0.5)  # 0.5 == default (not assessed)
    print(f"=== LLM fit-feature A/B — user '{user}', {len(ex)} labels "
          f"({n_pos} apply / {n_neg} pass) ===")
    print(f"coverage: {covered}/{len(ex)} postings have an LLM fit score "
          f"(rest default 0.5)\n")
    if covered < len(ex) * 0.5:
        print("WARNING: low coverage — run scripts.backfill_fit_scores first.\n")

    acc_b, auc_b = _kfold_eval(X_base, y, w)
    acc_a, auc_a = _kfold_eval(X_full, y, w)
    auc_llm_only = _auc(y, sims)

    print("--- 5-fold cross-validation ---")
    print(f"  WITHOUT llm_fit (6 features):   acc {acc_b:.1%}   AUC {auc_b:.3f}")
    print(f"  WITH llm_fit   (7 features):    acc {acc_a:.1%}   AUC {auc_a:.3f}")
    print(f"  llm_fit ALONE as ranker:                     AUC {auc_llm_only:.3f}")
    d = auc_a - auc_b
    print(f"\n  => llm_fit changes AUC by {d:+.3f} "
          f"({'helps' if d > 0.01 else 'no real lift' if abs(d) <= 0.01 else 'hurts'})")

    wts, b = _fit(X_full, y, w)
    print("\n--- learned weights with llm_fit (|weight| desc) ---")
    for name, wt in sorted(zip(FEATURES, wts), key=lambda t: -abs(t[1])):
        print(f"  {name:12} {wt:+.2f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "local")
