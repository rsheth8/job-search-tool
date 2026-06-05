"""Tailored resume cache: lookup, save, and reuse."""
from __future__ import annotations

from pathlib import Path

import pytest

from app import resume_store


def test_make_cache_key_normalizes():
    k1 = resume_store.make_cache_key("swe", "  Stripe ", "Backend Engineer")
    k2 = resume_store.make_cache_key("swe", "stripe", "backend engineer")
    assert k1 == k2
    assert k1.startswith("swe|")


def test_titles_similar():
    assert resume_store.titles_similar(
        "Backend Software Engineer", "Software Engineer"
    )
    assert resume_store.titles_similar(
        "Machine Learning Engineer", "ML Engineer"
    )
    assert not resume_store.titles_similar(
        "Backend Engineer", "Product Manager"
    )


def test_save_and_find_exact_match(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_TEX_DIR", str(tmp_path))
    from app import config

    config.get_settings.cache_clear()

    pdf = b"%PDF-1.4 fake one page"
    tex = r"\documentclass{article}\begin{document}Hi\end{document}"
    row = resume_store.save(
        "u1",
        "Stripe",
        "Backend Engineer",
        "swe",
        pdf_bytes=pdf,
        tex=tex,
        pages=1,
        posting_id=42,
    )
    assert Path(row["pdf_path"]).is_file()
    assert Path(row["tex_path"]).read_text() == tex

    hit = resume_store.find_cached(
        "u1", "Stripe", "Backend Engineer", "swe", posting_id=42
    )
    assert hit is not None
    assert hit["cache_key"] == row["cache_key"]
    assert resume_store.load_pdf(hit) == pdf


def test_find_reuses_similar_title_at_same_company(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_TEX_DIR", str(tmp_path))
    from app import config

    config.get_settings.cache_clear()

    resume_store.save(
        "u1",
        "Ramp",
        "Software Engineer",
        "swe",
        pdf_bytes=b"%PDF-swe",
        tex="tex",
        pages=1,
    )

    hit = resume_store.find_cached(
        "u1", "Ramp", "Backend Software Engineer", "swe"
    )
    assert hit is not None
    assert resume_store.load_pdf(hit) == b"%PDF-swe"


def test_find_misses_different_variant(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_TEX_DIR", str(tmp_path))
    from app import config

    config.get_settings.cache_clear()

    resume_store.save(
        "u1",
        "OpenAI",
        "ML Engineer",
        "aiml",
        pdf_bytes=b"%PDF-aiml",
        tex="tex",
        pages=1,
    )

    assert resume_store.find_cached(
        "u1", "OpenAI", "ML Engineer", "swe"
    ) is None
