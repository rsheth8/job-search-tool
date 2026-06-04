"""Job-source adapters: pull open postings from public job boards.

Each source exposes ``fetch(board_token) -> list[JobPosting]``. The free ATS
boards (Greenhouse, Lever, Ashby) need no auth. Adapters are deliberately
resilient — a network error, a bad token, or a shape change returns ``[]``
(logged) instead of raising, so one flaky board never breaks a discovery tick.

``fetch_source(source, token)`` dispatches by name; ``SOURCES`` lists the ones
wired up. Paid sources (aggregator, linkedin) are added later behind config
flags and slot into this same registry.
"""
from __future__ import annotations

import logging

from . import ashby, greenhouse, lever
from .base import JobPosting

logger = logging.getLogger("jobsources")

# name -> fetch(board_token) -> list[JobPosting]
SOURCES = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
}


def fetch_source(source: str, board_token: str) -> list[JobPosting]:
    """Fetch postings from one board. Unknown source or any error -> []."""
    fetcher = SOURCES.get((source or "").strip().lower())
    if fetcher is None:
        logger.warning("unknown job source %r — skipping", source)
        return []
    return fetcher(board_token)


__all__ = ["JobPosting", "SOURCES", "fetch_source"]
