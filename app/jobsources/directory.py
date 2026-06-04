"""Rotate through a curated ATS board directory — companies you didn't name.

Loads ``data/ats_boards.json`` (override via ``JOB_DIRECTORY_DATA_PATH``).
``board_token`` is unused at fetch time; ``fetch_directory_batch()`` advances a
global cursor and pulls the next N boards.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import get_settings
from . import greenhouse, lever, ashby
from .base import JobPosting

logger = logging.getLogger("jobsources.directory")

_FETCHERS = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
}


def _load_boards() -> dict[str, list[str]]:
    path = Path(get_settings().job_directory_data_path)
    if not path.is_absolute():
        root = Path(__file__).resolve().parents[2]
        path = root / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("directory data missing or invalid: %s", path)
        return {"greenhouse": [], "lever": [], "ashby": []}
    out: dict[str, list[str]] = {}
    for src in ("greenhouse", "lever", "ashby"):
        tokens = data.get(src) or []
        out[src] = [str(t).strip().lower() for t in tokens if str(t).strip()]
    return out


def _flat_boards() -> list[tuple[str, str]]:
    """(source, board_token) in stable round-robin order across sources."""
    boards = _load_boards()
    keys = [s for s in ("greenhouse", "lever", "ashby") if boards.get(s)]
    if not keys:
        return []
    max_len = max(len(boards[k]) for k in keys)
    pairs: list[tuple[str, str]] = []
    for i in range(max_len):
        for src in keys:
            lst = boards[src]
            if i < len(lst):
                pairs.append((src, lst[i]))
    return pairs


def fetch(board_token: str) -> list[JobPosting]:
    """Registry hook — use ``fetch_directory_batch`` from wide_discovery instead."""
    return []


def fetch_directory_batch(
    *,
    boards_to_probe: int | None = None,
    max_jobs_per_board: int | None = None,
) -> list[JobPosting]:
    """Probe the next slice of the directory and return normalized postings."""
    from .. import jobstore

    settings = get_settings()
    n_boards = boards_to_probe or settings.job_directory_boards_per_tick
    cap = max_jobs_per_board or settings.job_directory_max_jobs_per_board

    pairs = _flat_boards()
    if not pairs or n_boards <= 0:
        return []

    start = jobstore.get_directory_cursor() % len(pairs)
    selected: list[tuple[str, str]] = []
    for i in range(n_boards):
        selected.append(pairs[(start + i) % len(pairs)])
    jobstore.set_directory_cursor(start + n_boards)

    out: list[JobPosting] = []
    for src, token in selected:
        fetcher = _FETCHERS.get(src)
        if not fetcher:
            continue
        try:
            posts = fetcher(token)
        except Exception:  # noqa: BLE001
            logger.warning("directory probe failed %s/%s", src, token, exc_info=True)
            continue
        display = token.replace("-", " ").title()
        for p in posts[:cap]:
            p.company = p.company or display
            # Dedupe across sources: include board in external_id namespace.
            p.external_id = f"{src}:{token}:{p.external_id}"
            out.append(p)
    return out


def board_count() -> int:
    return len(_flat_boards())
