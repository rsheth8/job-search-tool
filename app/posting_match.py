"""Match discovered job postings to logged applications (company + role/title).

Used to suppress duplicate alerts after the user applies (including manual
\"applied stripe swe\" logging) and to namespace ATS external ids so the same
role discovered via tracked boards and the directory dedupes cleanly.
"""
from __future__ import annotations

import re

from .db import connect

_WORD = re.compile(r"[a-z0-9+#.]+")
_TITLE_STOP = frozenset({
    "the", "a", "an", "and", "or", "of", "at", "for", "to", "in",
    "i", "ii", "iii", "iv", "v",
    "senior", "staff", "lead", "principal", "junior", "sr", "jr", "level",
})
# Shorthand → phrases that should count as the same role (mirrors matcher.py).
_ROLE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "swe": ("software engineer",),
    "sde": ("software engineer",),
    "sdet": ("software engineer in test",),
    "ml": ("machine learning",),
    "ai": ("machine learning", "artificial intelligence"),
    "pm": ("product manager", "product management"),
    "tpm": ("technical program manager",),
}


def _expand_title_words(title: str) -> set[str]:
    words = _title_words(title)
    out = set(words)
    for w in list(words):
        for phrase in _ROLE_SYNONYMS.get(w, ()):
            out.update(_title_words(phrase))
    return out


def normalize_company(name: str | None) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def normalize_title(title: str | None) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def normalize_external_id(source: str, board_token: str, external_id: str) -> str:
    """Stable id: ``{source}:{board_token}:{job_id}`` (directory + tracked boards)."""
    ext = str(external_id).strip()
    src = (source or "").lower()
    token = (board_token or "").strip().lower()
    if not token:
        return ext
    prefix = f"{src}:{token}:"
    if ext.startswith(prefix):
        return ext
    return f"{prefix}{ext}"


def _title_words(title: str) -> set[str]:
    return {w for w in _WORD.findall(normalize_title(title)) if w not in _TITLE_STOP and len(w) > 1}


def titles_similar(a: str | None, b: str | None) -> bool:
    """True when two role/title strings likely describe the same opening."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 4 and len(nb) >= 4 and (na in nb or nb in na):
        return True
    wa, wb = _expand_title_words(na), _expand_title_words(nb)
    if not wa or not wb:
        return False
    overlap = len(wa & wb)
    union = len(wa | wb)
    return overlap >= 2 or (union > 0 and overlap / union >= 0.5)


def matches_application(
    app_company: str | None,
    app_role: str | None,
    posting_company: str | None,
    posting_title: str | None,
) -> bool:
    """True when a logged application covers this posting."""
    if not app_company or not posting_company:
        return False
    if normalize_company(app_company) != normalize_company(posting_company):
        return False
    if not app_role or not posting_title:
        return False
    return titles_similar(app_role, posting_title)


def user_already_applied_to(
    user_id: str, company: str | None, title: str | None
) -> bool:
    """True if the user has an application that covers this company + title."""
    if not company or not title:
        return False
    with connect() as conn:
        rows = conn.execute(
            "SELECT company, role FROM applications WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return any(
        matches_application(r["company"], r["role"], company, title) for r in rows
    )
