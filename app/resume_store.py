"""Persist tailored resumes on disk + SQLite for reuse across applies.

Reuse is narrow on purpose: the same posting, or the exact same company + title
+ job-description fingerprint. A nearby title at the same company is a new
résumé — those reqs are not the same job.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import get_settings
from .db import connect


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def jd_fingerprint(description: str | None) -> str:
    """Stable hash of the JD. Whitespace-only changes match; any other edit does not."""
    text = _normalize(description or "")
    if not text:
        return "nodesc"
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def make_cache_key(
    variant: str,
    company: str,
    title: str,
    description: str | None = None,
) -> str:
    return (
        f"{variant}|{_normalize(company)}|{_normalize(title)}|"
        f"{jd_fingerprint(description)}"
    )


def _fingerprint_from_key(cache_key: str) -> str | None:
    parts = (cache_key or "").split("|")
    if len(parts) >= 4:
        return parts[-1]
    return None


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
    description: str | None = None,
) -> sqlite3.Row | None:
    """Return a stored résumé only when it is this same job, else None.

    Same ``posting_id`` reuses (unless the stored JD hash disagrees with the
    description we have now). Without a posting id, company + title + JD
    fingerprint must match exactly. Similar titles at the same company do not.
    """
    want = jd_fingerprint(description)

    with connect() as conn:
        if posting_id is not None:
            rows = conn.execute(
                """
                SELECT * FROM tailored_resumes
                WHERE user_id = ? AND posting_id = ? AND variant = ?
                ORDER BY created_at DESC
                """,
                (user_id, posting_id, variant),
            ).fetchall()
            for row in rows:
                if not _row_usable(row):
                    continue
                stored = _fingerprint_from_key(row["cache_key"])
                # Legacy rows have no JD segment — only reuse if we also have no JD.
                if stored is None:
                    if want == "nodesc":
                        return row
                    continue
                if stored == want:
                    return row
            return None

        key = make_cache_key(variant, company, title, description)
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
    description: str | None = None,
) -> sqlite3.Row:
    """Write PDF + .tex to the volume and index in SQLite."""
    cache_key = make_cache_key(variant, company, title, description)
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
