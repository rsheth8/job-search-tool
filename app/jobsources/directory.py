"""Rotate through a curated ATS board directory — companies you didn't name.

Loads ``data/ats_boards.json`` (override via ``JOB_DIRECTORY_DATA_PATH``) and
merges any board tokens learned from apply URLs (swelist / RSS / YC).
When a profile is present, only boards whose catalog sector matches that
profile are probed (a marketing search does not rotate Stripe's board).
``board_token`` is unused at fetch time; ``fetch_directory_batch()`` advances a
cursor and pulls the next N boards.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .. import ats, catalog
from ..config import get_settings
from . import ashby, greenhouse, lever, smartrecruiters, workable
from .base import JobPosting

logger = logging.getLogger("jobsources.directory")

_FETCHERS = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
    "workable": workable.fetch,
    "smartrecruiters": smartrecruiters.fetch,
}

# Round-robin order. SmartRecruiters identifiers keep their original case.
ATS_SOURCES = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters")


def _norm_token(source: str, token: str) -> str:
    return ats.normalize_board_token(source, token)


def _empty_boards() -> dict[str, list[str]]:
    return {src: [] for src in ATS_SOURCES}


def _load_file_boards() -> dict[str, list[str]]:
    path = Path(get_settings().job_directory_data_path)
    if not path.is_absolute():
        root = Path(__file__).resolve().parents[2]
        path = root / path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("directory data missing or invalid: %s", path)
        return _empty_boards()
    out: dict[str, list[str]] = {}
    for src in ATS_SOURCES:
        tokens = data.get(src) or []
        seen: set[str] = set()
        cleaned: list[str] = []
        for raw in tokens:
            tok = _norm_token(src, str(raw))
            key = tok.lower()
            if not tok or key in seen:
                continue
            seen.add(key)
            cleaned.append(tok)
        out[src] = cleaned
    return out


def _load_boards() -> dict[str, list[str]]:
    boards = _load_file_boards()
    try:
        from .. import jobstore

        for src, token in jobstore.list_learned_boards():
            if src not in boards:
                continue
            tok = _norm_token(src, token)
            if not tok:
                continue
            existing = {t.lower() for t in boards[src]}
            if tok.lower() not in existing:
                boards[src].append(tok)
    except Exception:  # noqa: BLE001 — file-only directory still works
        logger.debug("directory: could not merge learned boards", exc_info=True)
    return boards


def _board_pair_key(source: str, token: str) -> tuple[str, str]:
    src = (source or "").strip().lower()
    tok = (token or "").strip()
    if src != "smartrecruiters":
        tok = tok.lower()
    return (src, tok)


def _flat_boards(sectors: frozenset[str] | None = None) -> list[tuple[str, str]]:
    """(source, board_token) in stable round-robin order across sources."""
    boards = _load_boards()
    keys = [s for s in ATS_SOURCES if boards.get(s)]
    if not keys:
        pairs: list[tuple[str, str]] = []
    else:
        max_len = max(len(boards[k]) for k in keys)
        pairs = []
        for i in range(max_len):
            for src in keys:
                lst = boards[src]
                if i < len(lst):
                    pairs.append((src, lst[i]))

    tagged = catalog.sector_index()
    if sectors:
        filtered: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for src, tok in pairs:
            key = _board_pair_key(src, tok)
            if key in seen:
                continue
            # Untagged file/learned boards default to software (this app's roots).
            sec = tagged.get(key, {"software"})
            if not (sec & set(sectors)):
                continue
            seen.add(key)
            filtered.append((src, tok))
        for src, tok in catalog.probe_pairs(sectors):
            key = _board_pair_key(src, tok)
            if key in seen:
                continue
            seen.add(key)
            filtered.append((src, tok))
        pairs = filtered
    return pairs


def fetch(board_token: str) -> list[JobPosting]:
    """Registry hook — use ``fetch_directory_batch`` from wide_discovery instead."""
    return []


def fetch_directory_batch(
    *,
    boards_to_probe: int | None = None,
    max_jobs_per_board: int | None = None,
    user_id: str = "",
    sectors: frozenset[str] | None = None,
) -> list[JobPosting]:
    """Probe the next slice of the directory and return normalized postings."""
    from .. import jobstore

    settings = get_settings()
    n_boards = boards_to_probe or settings.job_directory_boards_per_tick
    cap = max_jobs_per_board or settings.job_directory_max_jobs_per_board

    pairs = _flat_boards(sectors)
    if not pairs or n_boards <= 0:
        return []

    cursor_key = "directory:global"
    if user_id or sectors:
        sec = ",".join(sorted(sectors or [])) or "*"
        cursor_key = f"directory:{user_id or 'global'}:{sec}"
    start = jobstore.get_directory_cursor(cursor_key) % len(pairs)
    selected: list[tuple[str, str]] = []
    for i in range(n_boards):
        selected.append(pairs[(start + i) % len(pairs)])
    jobstore.set_directory_cursor(start + n_boards, cursor_key)

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


def board_count(sectors: frozenset[str] | None = None) -> int:
    return len(_flat_boards(sectors))


def learn_from_postings(postings: list[JobPosting]) -> int:
    """Persist new ATS board tokens found on apply URLs. Returns newly added."""
    from .. import jobstore

    added = 0
    seen: set[tuple[str, str]] = set()
    for p in postings:
        hit = ats.board_from_url(p.url)
        if not hit:
            continue
        src, token = hit
        if src not in _FETCHERS:
            continue
        key = (src, token.lower())
        if key in seen:
            continue
        seen.add(key)
        if jobstore.add_learned_board(src, token):
            added += 1
    if added:
        logger.info("directory: learned %d new board(s) from apply URLs", added)
    return added
