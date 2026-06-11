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


def _embed_local(texts: list[str]) -> list[list[float]]:
    from model2vec import StaticModel

    print("  loading local embedding model (minishlab/potion-base-8M)…")
    model = StaticModel.from_pretrained("minishlab/potion-base-8M")
    return [list(map(float, v)) for v in model.encode(texts)]


def _embed_voyage(texts: list[str], input_type: str) -> list[list[float]]:
    """Real Voyage embeddings via app.embeddings. Small chunks + backoff so the
    free tier (~3 req/min, token-per-min cap) doesn't 429. Missing -> []."""
    import time

    from app import embeddings

    out: list[list[float]] = []
    chunk = 12  # smaller: full-text postings are ~400 tokens each (free-tier TPM)
    for i in range(0, len(texts), chunk):
        batch = texts[i:i + chunk]
        vecs = [None] * len(batch)
        for attempt in range(5):
            vecs = embeddings.embed(batch, input_type=input_type)
            if any(v is not None for v in vecs):
                break
            time.sleep(25)  # 429 backoff
        out.extend(v if v is not None else [] for v in vecs)
        print(f"    embedded {min(i + chunk, len(texts))}/{len(texts)}")
        if i + chunk < len(texts):
            time.sleep(22)  # space requests for free-tier RPM
    return out


def _cos(a, b) -> float:
    from app.embeddings import cosine
    return cosine(a, b)


def main(user: str = "local") -> None:
    prof = profile.get_profile(user)
    ex = _labeled_examples(user)
    if not ex:
        print(f"No labels for '{user}'."); return
    feat = Featurizer(prof)

    # Base 6-feature matrix (current model) + labels/weights.
    X_base, y, w = [], [], []
    texts = []
    for title, loc, desc, source, external_id, rel, label, weight in ex:
        X_base.append(feat.features(title=title, location=loc, description=desc,
                                    source=source, relevance=rel, external_id=external_id))
        y.append(label); w.append(weight)
        texts.append(f"{title}\n{loc}\n{(desc or '')[:1500]}")

    from app.config import get_settings
    prof_text = profile_text(prof) or "software engineer data scientist"
    if get_settings().embedding_active:
        print(f"=== Embedding A/B — user '{user}', {len(ex)} labels  [Voyage: "
              f"{get_settings().embedding_model}] ===\n")
        doc_vecs = _embed_voyage(texts, "document")
        pvec = _embed_voyage([prof_text], "query")[0]
    else:
        print(f"=== Embedding A/B — user '{user}', {len(ex)} labels  [local model2vec] ===\n")
        allv = _embed_local(texts + [prof_text])
        doc_vecs, pvec = allv[:-1], allv[-1]
    sims = [_cos(doc_vecs[i], pvec) for i in range(len(ex))]
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
