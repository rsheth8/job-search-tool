"""What would JOB_RELEVANCE_THRESHOLD actually do, measured on your own labels?

The threshold is documented as a taste setting ("be less picky"), which makes it easy
to guess at. It doesn't have to be a guess: you have swipe labels, and every labelled
posting also has the score the threshold gates on. Join them and the tradeoff at each
candidate threshold is measurable.

    DATABASE_PATH=job_search.db python -m scripts.threshold_report local

Reads two things and writes nothing:
  * ``job_postings.relevance_score`` — the score the gate actually compares, i.e.
    *after* re-ranking when the re-ranker is on. This is the one that matters;
    ``training_labels.relevance_score`` is the base matcher score captured at swipe
    time and is a much weaker signal (the report prints both so you can see the gap).
  * ``training_labels.label`` — your like/pass.

⚠️ Two honest caveats on the numbers, both of which make them *optimistic*:

  1. The re-ranker was trained on these same labels, so its AUC here is in-sample.
     ``reranker.py``'s promotion guard is the honest held-out measure.
  2. Swipe labels come from the trainer's decks, not from a random sample of what
     discovery surfaced, so the base rate is not the base rate of the firehose.

Both biases hit every threshold equally, so *comparing* two thresholds is far more
trustworthy than any single precision number. Read the shape, not the absolutes.
"""
from __future__ import annotations

import sys

from app import db

THRESHOLDS = (0.50, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90)


def _auc(pos: list[float], neg: list[float]) -> float:
    """P(a random like outranks a random pass), ties at half credit. 0.5 = coin flip.

    O(n·m) on purpose — these are hundreds of labels, and the explicit form is easier
    to trust than a rank-based shortcut."""
    if not pos or not neg:
        return float("nan")
    wins = ties = 0
    for p in pos:
        for n in neg:
            if p > n:
                wins += 1
            elif p == n:
                ties += 1
    return (wins + 0.5 * ties) / (len(pos) * len(neg))


def _split(rows: list, score_key: str) -> tuple[list[float], list[float]]:
    pos = [r[score_key] for r in rows if r["label"] == "like"]
    neg = [r[score_key] for r in rows if r["label"] == "pass"]
    return pos, neg


def report(user_id: str) -> int:
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT p.relevance_score AS gate,
                      t.relevance_score AS base,
                      t.label
                 FROM training_labels t
                 JOIN job_postings p
                   ON p.external_id = t.external_id
                  AND p.source      = t.source
                  AND p.user_id     = t.user_id
                WHERE t.user_id = ? AND p.relevance_score IS NOT NULL""",
            (user_id,),
        )]

    if not rows:
        print(f"No labelled postings for '{user_id}'. Swipe some at /train first.")
        return 1

    pos, neg = _split(rows, "gate")
    if not pos or not neg:
        print(f"Need both likes and passes to compare thresholds "
              f"(have {len(pos)} like / {len(neg)} pass).")
        return 1

    print(f"user={user_id}  labelled postings with a gating score: {len(rows)}")
    print(f"  {len(pos)} like / {len(neg)} pass  —  base rate {len(pos) / len(rows):.0%}")
    print()

    base_pos, base_neg = _split([r for r in rows if r["base"] is not None], "base")
    print(f"  AUC, base matcher score : {_auc(base_pos, base_neg):.3f}")
    print(f"  AUC, gating score       : {_auc(pos, neg):.3f}   <- what the gate sees")
    print("  (0.50 = coin flip. The gap between these two lines is what re-ranking buys.)")
    print()

    print("  Alerting at each threshold, on these labels:")
    print(f"  {'T':>5} {'alerts':>7} {'good':>6} {'precision':>10} {'recall':>8}")
    for t in THRESHOLDS:
        good = sum(1 for x in pos if x >= t)
        bad = sum(1 for x in neg if x >= t)
        total = good + bad
        prec = good / total if total else 0.0
        print(f"  {t:>5.2f} {total:>7} {good:>6} {prec:>9.0%} {good / len(pos):>7.0%}")

    print()
    print("  Recall is the share of jobs you liked that you'd still be told about.")
    print("  Look for a row that raises precision without dropping recall much —")
    print("  that's a free win. A row that trades a lot of recall for a little")
    print("  precision is a taste call, and only you can make it.")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    return report(argv[0])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
