"""Persist tailored resumes on disk + SQLite for reuse across applies.

Each cached resume is keyed by user, variant (swe/aiml), company, and role title.
Lookup also matches the same posting or a similar title at the same company so
we don't re-run Claude + Tectonic when we already have a good fit on file.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import get_settings
from .db import connect

_STOP_TITLE_WORDS = frozenset({"the", "a", "an", "at", "and", "or", "of", "for", "to"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def make_cache_key(variant: str, company: str, title: str) -> str:
    return f"{variant}|{_normalize(company)}|{_normalize(title)}"


def _title_tokens(title: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+", (title or "").lower()))
    words -= _STOP_TITLE_WORDS
    if "ml" in words:
        words.update(("machine", "learning"))
    if "ai" in words:
        words.update(("artificial", "intelligence"))
    return words


def titles_similar(a: str, b: str, *, threshold: float = 0.5) -> bool:
    """True when two role titles overlap enough to reuse the same resume."""
    na, nb = _normalize(a), _normalize(b)
    if na == nb or na in nb or nb in na:
        return True
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return False
    if ta <= tb or tb <= ta:
        return True
    return len(ta & tb) / len(ta | tb) >= threshold


def _storage_root() -> Path:
    return Path(get_settings().resume_tex_dir) / "tailored"


def _entry_dir(user_id: str, cache_key: str) -> Path:
    digest = hashlib.sha256(cache_key.encode()).hexdigest()[:20]
    return _storage_root() / user_id / digest


def find_cached(
    user_id: str,
    company: str,
    title: str,
    variant: str,
    *,
    posting_id: int | None = None,
) -> sqlite3.Row | None:
    """Return a stored resume row if one fits this apply, else None."""
    key = make_cache_key(variant, company, title)

    with connect() as conn:
        if posting_id is not None:
            row = conn.execute(
                """
                SELECT * FROM tailored_resumes
                WHERE user_id = ? AND posting_id = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id, posting_id),
            ).fetchone()
            if row and _row_usable(row):
                return row

        row = conn.execute(
            """
            SELECT * FROM tailored_resumes
            WHERE user_id = ? AND cache_key = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, key),
        ).fetchone()
        if row and _row_usable(row):
            return row

        # Same company + variant, similar title (e.g. "Backend Engineer" vs "Software Engineer").
        candidates = conn.execute(
            """
            SELECT * FROM tailored_resumes
            WHERE user_id = ? AND variant = ? AND lower(company) = lower(?)
            ORDER BY created_at DESC
            """,
            (user_id, variant, company),
        ).fetchall()
        for row in candidates:
            if titles_similar(title, row["title"] or "") and _row_usable(row):
                return row

    return None


def _row_usable(row: sqlite3.Row) -> bool:
    pdf = Path(row["pdf_path"])
    tex = Path(row["tex_path"])
    return (
        pdf.is_file()
        and tex.is_file()
        and row["pages"] == 1
    )


def load_pdf(row: sqlite3.Row) -> bytes:
    return Path(row["pdf_path"]).read_bytes()


def save(
    user_id: str,
    company: str,
    title: str,
    variant: str,
    *,
    pdf_bytes: bytes,
    tex: str,
    pages: int,
    posting_id: int | None = None,
) -> sqlite3.Row:
    """Write PDF + .tex to the volume and index in SQLite."""
    cache_key = make_cache_key(variant, company, title)
    dest = _entry_dir(user_id, cache_key)
    dest.mkdir(parents=True, exist_ok=True)

    pdf_path = dest / "resume.pdf"
    tex_path = dest / "resume.tex"
    pdf_path.write_bytes(pdf_bytes)
    tex_path.write_text(tex, encoding="utf-8")

    now = _now_iso()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO tailored_resumes (
                user_id, cache_key, company, title, variant,
                pdf_path, tex_path, posting_id, pages, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, cache_key) DO UPDATE SET
                company = excluded.company,
                title = excluded.title,
                pdf_path = excluded.pdf_path,
                tex_path = excluded.tex_path,
                posting_id = COALESCE(excluded.posting_id, tailored_resumes.posting_id),
                pages = excluded.pages,
                created_at = excluded.created_at
            """,
            (
                user_id,
                cache_key,
                company,
                title,
                variant,
                str(pdf_path),
                str(tex_path),
                posting_id,
                pages,
                now,
            ),
        )
        return conn.execute(
            "SELECT * FROM tailored_resumes WHERE user_id = ? AND cache_key = ?",
            (user_id, cache_key),
        ).fetchone()
