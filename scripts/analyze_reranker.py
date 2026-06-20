"""Offline analysis of the personalized re-ranker for a user.

Reports: label balance, the saved model, cross-validated accuracy/AUC vs a
majority baseline, learned feature weights, and a learning curve (how the model
improved as labels accumulated). Read-only; trains throwaway models in memory.

Usage:  python -m scripts.analyze_reranker [user_id]
"""
from __future__ import annotations

import sys

from app import profile, reranker
from app.reranker import FEATURES, Featurizer, _build_dataset, _fit, _labeled_examples, _predict


def _auc(y, p) -> float:
    pos = [pi for yi, pi in zip(y, p) if yi >= 0.5]
    neg = [pi for yi, pi in zip(y, p) if yi < 0.5]
    if not pos or not neg:
        return float("nan")
    wins = sum((a > b) + 0.5 * (a == b) for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def _kfold_eval(X, y, w, k=5):
    """Manual k-fold: accuracy + AUC on held-out folds (no sklearn)."""
    n = len(X)
    idx = list(range(n))
    # Deterministic interleave keeps classes spread across folds.
    idx.sort(key=lambda i: (y[i], i))
    folds = [idx[i::k] for i in range(k)]
    accs, preds_all, ys_all = [], [], []
    for f in range(k):
        test = set(folds[f])
        tr = [i for i in idx if i not in test]
        te = [i for i in idx if i in test]
        if not te or len({y[i] for i in tr}) < 2:
            continue
        wts, b = _fit([X[i] for i in tr], [y[i] for i in tr], [w[i] for i in tr])
        correct = 0
        for i in te:
            pr = _predict(wts, b, X[i])
            preds_all.append(pr); ys_all.append(y[i])
            correct += int((pr >= 0.5) == (y[i] >= 0.5))
        accs.append(correct / len(te))
    acc = sum(accs) / len(accs) if accs else float("nan")
    return acc, _auc(ys_all, preds_all)


def _learning_curve(X, y, w):
    """Train on the first N examples, test on the remaining tail."""
    n = len(X)
    rows = []
    for frac in (0.25, 0.5, 0.75, 1.0):
        cut = max(10, int(n * frac))
        if cut >= n and frac < 1.0:
            continue
        Xtr, ytr, wtr = X[:cut], y[:cut], w[:cut]
        if len({yi for yi in ytr}) < 2:
            continue
        if frac < 1.0 and cut < n:
            Xte, yte = X[cut:], y[cut:]
        else:  # whole set: report training-fit accuracy
            Xte, yte = X, y
        wts, b = _fit(Xtr, ytr, wtr)
        acc = sum(int((_predict(wts, b, xi) >= 0.5) == (yi >= 0.5))
                  for xi, yi in zip(Xte, yte)) / len(Xte)
        rows.append((cut, len(Xte), acc))
    return rows


def main(user: str = "local") -> None:
    prof = profile.get_profile(user)
    examples = _labeled_examples(user)
    if not examples:
        print(f"No labels for user '{user}'."); return
    feat = Featurizer(prof)
    X, y, w, n_pos, n_neg = _build_dataset(examples, feat)
    n = len(X)

    print(f"=== Re-ranker analysis — user '{user}' ===\n")
    print(f"Labels: {n}   ({n_pos} would-apply / {n_neg} pass)")
    base = max(n_pos, n_neg) / n
    print(f"Class balance: {n_pos/n:.0%} positive  →  majority-guess baseline accuracy = {base:.1%}\n")

    saved = reranker.load_model(user)
    if saved:
        print(f"Saved model: trained on {saved['n_labels']} labels at {saved['trained_at'][:19]}")
    print("Engages in discovery:", "yes" if (n_pos >= 5 and n_neg >= 5) else "no (need >=5 each)")

    print("\n--- 5-fold cross-validation (how well it predicts YOUR swipes) ---")
    acc, auc = _kfold_eval(X, y, w)
    print(f"  accuracy : {acc:.1%}   (vs {base:.1%} just guessing the majority)")
    print(f"  ROC-AUC  : {auc:.3f}   (0.5 = random, 1.0 = perfect ranking)")

    print("\n--- Learned feature weights (full model; + favors apply) ---")
    wts, b = _fit(X, y, w)
    for name, wt in sorted(zip(FEATURES, wts), key=lambda t: -abs(t[1])):
        bar = "#" * min(30, int(abs(wt) * 8))
        print(f"  {name:12} {wt:+.2f}  {bar}")
    print(f"  {'(bias)':12} {b:+.2f}")

    print("\n--- Learning curve (more labels = better? 'now vs before') ---")
    for cut, ntest, acc in _learning_curve(X, y, w):
        print(f"  trained on first {cut:3d} -> {acc:.1%} accuracy on {ntest} held-out")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "local")
