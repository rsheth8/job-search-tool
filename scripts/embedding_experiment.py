"""A/B experiment: does adding a semantic-embedding feature improve the re-ranker?

Embeds each labeled posting (title + description) and the candidate profile with a
LOCAL static embedding model (model2vec — no API, stand-in for Voyage), computes
cosine similarity, and compares the re-ranker WITH vs WITHOUT that feature on
5-fold cross-validated AUC/accuracy. Read-only.

Run:  python -m scripts.embedding_experiment [user_id]
(requires: pip install model2vec)
"""
from __future__ import annotations

import math
import sys

from app import profile
from app.profile import profile_text
from app.reranker import FEATURES, Featurizer, _fit, _labeled_examples, _predict
from scripts.analyze_reranker import _auc, _kfold_eval


def _embed(texts: list[str]):
    from model2vec import StaticModel

    print("  loading local embedding model (minishlab/potion-base-8M)…")
    model = StaticModel.from_pretrained("minishlab/potion-base-8M")
    return model.encode(texts)


def _cos(a, b) -> float:
    dot = float((a * b).sum())
    na = math.sqrt(float((a * a).sum())); nb = math.sqrt(float((b * b).sum()))
    return dot / (na * nb) if na and nb else 0.0


def main(user: str = "local") -> None:
    prof = profile.get_profile(user)
    ex = _labeled_examples(user)
    if not ex:
        print(f"No labels for '{user}'."); return
    feat = Featurizer(prof)

    # Base 6-feature matrix (current model) + labels/weights.
    X_base, y, w = [], [], []
    texts = []
    for title, loc, desc, source, rel, label, weight in ex:
        X_base.append(feat.features(title=title, location=loc, description=desc,
                                    source=source, relevance=rel))
        y.append(label); w.append(weight)
        texts.append(f"{title}\n{loc}\n{(desc or '')[:600]}")

    print(f"=== Embedding A/B — user '{user}', {len(ex)} labels ===\n")
    vecs = _embed(texts + [profile_text(prof) or "software engineer data scientist"])
    pvec = vecs[-1]
    sims = [_cos(vecs[i], pvec) for i in range(len(ex))]
    lo, hi = min(sims), max(sims)
    print(f"  embedding cosine range: {lo:.3f} … {hi:.3f}\n")

    # Augmented matrix: base + embedding similarity as a 7th feature.
    X_aug = [row + [sims[i]] for i, row in enumerate(X_base)]

    acc_b, auc_b = _kfold_eval(X_base, y, w)
    acc_a, auc_a = _kfold_eval(X_aug, y, w)
    auc_embed_only = _auc(y, sims)  # can semantic similarity ALONE rank applies?

    print("--- 5-fold cross-validation ---")
    print(f"  WITHOUT embeddings (current 6 features):  acc {acc_b:.1%}   AUC {auc_b:.3f}")
    print(f"  WITH embedding feature (7 features):      acc {acc_a:.1%}   AUC {auc_a:.3f}")
    print(f"  embedding similarity ALONE as ranker:                  AUC {auc_embed_only:.3f}")
    d = auc_a - auc_b
    print(f"\n  => embedding feature changes AUC by {d:+.3f} "
          f"({'helps' if d > 0.01 else 'no real lift' if abs(d) <= 0.01 else 'hurts'})")

    # Where does the model put the embedding feature's weight?
    wts, b = _fit(X_aug, y, w)
    print("\n--- learned weights with embedding added (|weight| desc) ---")
    for name, wt in sorted(zip(list(FEATURES) + ["embed_sim"], wts), key=lambda t: -abs(t[1])):
        print(f"  {name:12} {wt:+.2f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "local")
