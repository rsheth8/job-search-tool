"""GitHub project catalog — pick the two that fit a posting.

The base resumes only have room for two projects. This catalog holds every
resume-worthy repo (homework/tutorials omitted). ``pick`` scores them against
the job text; ``inject`` swaps the KEY PROJECTS section before Claude edits.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

_CATALOG = Path(__file__).resolve().parents[1] / "data" / "projects.json"

_DEFAULTS = {
    "swe": ("pantrypal", "mydrive"),
    "aiml": ("distill", "songsift"),
}

_PROJECTS_N = 2


@lru_cache(maxsize=1)
def load_catalog() -> list[dict]:
    if not _CATALOG.is_file():
        return []
    try:
        data = json.loads(_CATALOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    projects = data.get("projects") if isinstance(data, dict) else data
    return [p for p in (projects or []) if p.get("id") and p.get("name")]


def by_id(pid: str) -> dict | None:
    for p in load_catalog():
        if p["id"] == pid:
            return p
    return None


def pick(
    title: str,
    description: str | None,
    variant: str = "swe",
    *,
    n: int = _PROJECTS_N,
) -> list[dict]:
    """Top ``n`` projects for this posting. Falls back to variant defaults."""
    catalog = load_catalog()
    if not catalog:
        return []
    ranked = sorted(
        catalog,
        key=lambda p: score(p, title, description, variant),
        reverse=True,
    )
    # A real keyword hit scores ~1+. Track-bonus-only rows are padding fodder.
    matched = [p for p in ranked if score(p, title, description, variant) >= 0.9]
    top = matched[:n]
    if len(top) >= n:
        return top[:n]
    # Pad with variant defaults so the resume never has a single lonely project.
    seen = {p["id"] for p in top}
    for pid in _DEFAULTS.get(variant, _DEFAULTS["swe"]):
        if pid in seen:
            continue
        row = by_id(pid)
        if row is not None:
            top.append(row)
            seen.add(pid)
        if len(top) >= n:
            break
    if len(top) < n:
        for p in catalog:
            if p["id"] not in seen:
                top.append(p)
                seen.add(p["id"])
            if len(top) >= n:
                break
    return top[:n]


def score(project: dict, title: str, description: str | None, variant: str) -> float:
    """Higher is better. Keyword *hits* dominate so a long tag list doesn't dilute a real match."""
    context = f"{title or ''} {description or ''}".lower()
    keywords = [str(k).lower() for k in (project.get("keywords") or []) if k]
    kw_hits = 0.0
    for k in keywords:
        if k in context:
            kw_hits += 1.0
            continue
        # "react native" still counts on a JD that only says "React"
        parts = [p for p in k.split() if len(p) >= 4]
        if parts and all(p in context for p in parts):
            kw_hits += 1.0
        elif parts and any(p in context for p in parts):
            kw_hits += 0.4

    blob = " ".join([
        str(project.get("name") or ""),
        str(project.get("tagline") or ""),
        " ".join(keywords),
    ]).lower()
    words = set(re.findall(r"[a-z0-9+#.]{3,}", blob))
    ctx = set(re.findall(r"[a-z0-9+#.]{3,}", context))
    overlap = (len(words & ctx) / len(words)) if words and ctx else 0.0

    tracks = [str(t).lower() for t in (project.get("tracks") or [])]
    track_bonus = 0.25 if (variant or "swe").lower() in tracks else 0.0
    return kw_hits + overlap * 0.3 + track_bonus


def render_tex(projects: list[dict]) -> str:
    blocks = []
    for p in projects:
        name = p["name"]
        tagline = p.get("tagline") or ""
        dates = p.get("dates") or ""
        bullets = p.get("latex_bullets") or []
        items = "\n".join(f"  \\item {b}" for b in bullets)
        blocks.append(
            f"\\textbf{{{name}}} -- \\textit{{{tagline}}} \\hfill \\textit{{{dates}}}\n"
            f"\\begin{{itemize}}\n{items}\n\\end{{itemize}}"
        )
    return "\n\n".join(blocks) + ("\n\n" if blocks else "")


def inject(tex: str, projects: list[dict]) -> str:
    """Replace the KEY PROJECTS body with ``projects``. No-op if the section is missing."""
    if not projects:
        return tex
    start = tex.find(r"\section{KEY PROJECTS}")
    if start < 0:
        return tex
    next_sec = tex.find(r"\section{", start + 1)
    if next_sec < 0:
        return tex
    header_end = tex.find("\n\n", start)
    if header_end < 0 or header_end > next_sec:
        header_end = tex.find("\n", start)
    if header_end < 0 or header_end > next_sec:
        return tex
    return tex[: header_end + 2] + render_tex(projects) + tex[next_sec:]


def knowledge_summaries() -> list[tuple[str, str]]:
    """(label, summary) for seeding the personal knowledge store."""
    return [
        (p["name"], p["summary"])
        for p in load_catalog()
        if p.get("summary")
    ]
