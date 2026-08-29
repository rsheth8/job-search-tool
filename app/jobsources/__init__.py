"""Job-source adapters: pull open postings from public job boards.

Each source exposes ``fetch(board_token) -> list[JobPosting]``. The free ATS
boards (Greenhouse, Lever, Ashby, Workable, SmartRecruiters) need no auth.
Adapters are deliberately resilient — a network error, a bad token, or a
shape change returns ``[]`` (logged) instead of raising, so one flaky board
never breaks a discovery tick.

``fetch_source(source, token)`` dispatches by name; ``SOURCES`` lists the ones
wired up.
"""
from __future__ import annotations

import logging

from . import ashby, directory, greenhouse, lever, rss, smartrecruiters, swelist, workable, yc
from .base import JobPosting

logger = logging.getLogger("jobsources")

# name -> fetch(board_token) -> list[JobPosting]
SOURCES = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
    "workable": workable.fetch,
    "smartrecruiters": smartrecruiters.fetch,
    "rss": rss.fetch,
    "directory": directory.fetch,
    "swelist": swelist.fetch,
    "yc": yc.fetch,
}

# Sources whose ``board_token`` is a URL/search query/cursor, not a per-company
# slug. ``resolve_board`` must never slug-probe these.
NON_BOARD_SOURCES = frozenset({"rss", "directory", "swelist", "yc"})


def fetch_source(source: str, board_token: str) -> list[JobPosting]:
    """Fetch postings from one board. Unknown source or any error -> []."""
    fetcher = SOURCES.get((source or "").strip().lower())
    if fetcher is None:
        logger.warning("unknown job source %r — skipping", source)
        return []
    return fetcher(board_token)


__all__ = ["JobPosting", "SOURCES", "NON_BOARD_SOURCES", "fetch_source"]
