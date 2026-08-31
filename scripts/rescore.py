#!/usr/bin/env python3
"""Recompute relevance_score for postings already in the database.

Discovery scores a posting once, when it first sees it, and never again — the
right default (scoring is the expensive step, and a stable number means a stable
feed). The cost is that a scorer change, or a profile edit, only moves postings
found *after* it. An account that already has a few hundred rows keeps whatever
the old code decided about them.

This is the escape hatch for that. It re-runs the *free heuristic* over stored
postings; it never calls the paid model, so it is safe to run on prod and costs
nothing. LLM-scored rows are left alone unless you pass --include-llm, because
overwriting a model's judgement with a heuristic is a downgrade.

Usage:
    .venv/bin/python -m scripts.rescore --user usr_abc123 --dry-run
    .venv/bin/python -m scripts.rescore --user usr_abc123
    .venv/bin/python -m scripts.rescore --all
    .venv/bin/python -m scripts.rescore --all --min-change 0.05

On Fly:
    fly ssh console -a job-search-tool -C "cd /app && python -m scripts.rescore --all"
"""
from __future__ import annotations

import argparse
import sys

from app import matcher, profile
from app.db import connect
from app.jobstore import JobPosting


def _postings_for(user_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, source, external_id, company, title, location, url, "
            "description, relevance_score, status "
            "FROM job_postings WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _users_with_postings() -> list[str]:
    with connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT user_id FROM job_postings ORDER BY user_id")]


def rescore_user(user_id: str, *, dry_run: bool, min_change: float,
                 verbose: bool = False) -> tuple[int, int]:
    """Returns (considered, changed)."""
    prof = profile.get_profile(user_id)
    if prof is None:
        print(f"  {user_id}: no search profile — skipped "
              f"(scores would all be the neutral 0.5)")
        return 0, 0

    rows = _postings_for(user_id)
    if not rows:
        return 0, 0

    posts = [
        JobPosting(
            source=r["source"], external_id=r["external_id"],
            company=r["company"], title=r["title"],
            location=r["location"] or "", url=r["url"] or "",
            description=r["description"] or "",
        )
        for r in rows
    ]
    # allow_llm=False: free heuristic only. This must never spend money, or it
    # is not something anyone will run on production.
    scored = matcher.score(posts, prof, allow_llm=False)

    updates: list[tuple[float, int]] = []
    for row, (_, new) in zip(rows, scored):
        old = row["relevance_score"]
        if old is not None and abs(new - old) < min_change:
            continue
        updates.append((round(new, 3), row["id"]))
        if verbose:
            arrow = "→"
            print(f"    {old if old is None else f'{old:.3f}'} {arrow} {new:.3f}  "
                  f"{(row['company'] or '?')} — {(row['title'] or '')[:48]}")

    if updates and not dry_run:
        with connect() as conn:
            conn.executemany(
                "UPDATE job_postings SET relevance_score = ? WHERE id = ?",
                updates,
            )

    verb = "would change" if dry_run else "changed"
    print(f"  {user_id}: {len(rows)} posting(s), {verb} {len(updates)}")
    return len(rows), len(updates)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = ap.add_mutually_exclusive_group(required=True)
    target.add_argument("--user", help="user id to rescore")
    target.add_argument("--all", action="store_true",
                        help="every user that has postings")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    ap.add_argument("--min-change", type=float, default=0.001,
                    help="skip rows moving less than this (default 0.001)")
    ap.add_argument("--verbose", action="store_true",
                    help="print every changed posting")
    args = ap.parse_args()

    users = _users_with_postings() if args.all else [args.user]
    if not users:
        print("No users with postings.")
        return 0

    if args.dry_run:
        print("DRY RUN — nothing will be written.\n")

    considered = changed = 0
    for uid in users:
        c, ch = rescore_user(uid, dry_run=args.dry_run,
                             min_change=args.min_change, verbose=args.verbose)
        considered += c
        changed += ch

    verb = "would change" if args.dry_run else "changed"
    print(f"\n{len(users)} user(s), {considered} posting(s), {verb} {changed}.")
    if args.dry_run and changed:
        print("Re-run without --dry-run to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
