"""Backfill LLM fit scores (the hybrid re-ranker feature) for labeled postings.

Runs the card summarizer over every already-labeled posting so each gets a cached
``fit_score`` in ``posting_summaries``. Needs ANTHROPIC_API_KEY. Chunked so the
JSON doesn't truncate; bypasses the daily-cap (passes the summarizer explicitly)
but respects the per-minute limiter. Idempotent — cached postings are skipped.

Run:  python -m scripts.backfill_fit_scores [user_id]
"""
from __future__ import annotations

import sys
import time

from app import insights, profile
from app.db import connect
from app.profile import profile_text


def _labeled_cards(user: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT source, external_id, company, title, description FROM training_labels
            WHERE user_id = ?
            UNION
            SELECT source, external_id, company, title, description FROM job_postings
            WHERE user_id = ? AND status IN ('applied', 'dismissed', 'snoozed')
            """,
            (user, user),
        ).fetchall()
    return [dict(r) for r in rows]


def main(user: str = "local") -> None:
    cards = _labeled_cards(user)
    pblock = profile_text(profile.get_profile(user))
    if not cards:
        print(f"No labeled postings for '{user}'."); return

    pre = len(insights.cached_fit_scores(
        [f"{c['source']}:{c['external_id']}" for c in cards]))
    print(f"{len(cards)} labeled postings; {pre} already have a fit score.\n")

    done = 0
    for i in range(0, len(cards), 10):
        chunk = cards[i:i + 10]
        # Pass the summarizer explicitly -> bypasses the daily cap (a one-off
        # backfill), still respects the per-minute rate limiter inside it.
        insights.enrich(chunk, pblock, summarize=insights.summarize_batch)
        done += len(chunk)
        print(f"  summarized {done}/{len(cards)}")
        time.sleep(1)

    post = len(insights.cached_fit_scores(
        [f"{c['source']}:{c['external_id']}" for c in cards]))
    print(f"\nfit scores cached: {pre} -> {post} of {len(cards)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "local")
