"""Turn catalog *names* into live ATS boards, a few per discovery tick.

``data/company_catalog.json`` holds thousands of employer names and only a
hundred-odd known tokens. Overnight we slug-probe names in the user's sector
against Greenhouse / Lever / Ashby / Workable / SmartRecruiters, keep tokens
that actually return jobs, and persist them via ``directory_learned_boards``
so the rotating directory grows. Capped per tick — never poll the whole book.
"""
from __future__ import annotations

import logging
import sqlite3

from . import catalog, jobstore
from .config import get_settings
from .jobsources import NON_BOARD_SOURCES, fetch_source

logger = logging.getLogger("catalog_probe")

_SLUGS_PER_NAME = 2


def probe_for_user(user_id: str, prof: sqlite3.Row | None) -> int:
    """Probe the next batch of catalog names for this profile. Returns boards learned."""
    settings = get_settings()
    if not settings.job_catalog_probe_enabled:
        return 0
    sectors = catalog.directory_sectors(prof)
    names = [
        n for n in catalog.names_for_sectors(sectors)
        if n and not catalog.lookup_board(n)
    ]
    if not names:
        return 0
    cap = max(1, int(settings.job_catalog_probe_per_tick or 1))
    key = f"catalog_probe:{user_id}:{','.join(sorted(sectors))}"
    pos = jobstore.get_directory_cursor(key) % len(names)
    batch = [names[(pos + i) % len(names)] for i in range(min(cap, len(names)))]
    sources = [s for s in settings.job_sources if s not in NON_BOARD_SOURCES]
    learned = 0
    for name in batch:
        hit = _probe_name(name, sources)
        if not hit:
            continue
        if jobstore.add_learned_board(hit["source"], hit["board_token"]):
            learned += 1
            logger.info(
                "catalog probe: learned %s/%s (%s)",
                hit["source"], hit["board_token"], name,
            )
    jobstore.set_directory_cursor((pos + cap) % len(names), key)
    return learned


def _probe_name(name: str, sources: list[str]) -> dict | None:
    from .discovery import _slug_variants

    for slug in _slug_variants(name)[:_SLUGS_PER_NAME]:
        for source in sources:
            posts = fetch_source(source, slug)
            if posts:
                return {
                    "source": source,
                    "board_token": slug,
                    "company_name": name,
                }
    return None
