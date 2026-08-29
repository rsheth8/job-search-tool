"""Tailored resume cache: lookup, save, and reuse."""
from __future__ import annotations

from pathlib import Path

from app import resume_store


def test_make_cache_key_includes_jd_hash():
    k1 = resume_store.make_cache_key("swe", "  Stripe ", "Backend Engineer", "python apis")
    k2 = resume_store.make_cache_key("swe", "stripe", "backend engineer", "python apis")
    k3 = resume_store.make_cache_key("swe", "stripe", "backend engineer", "golang kubernetes")
    assert k1 == k2
    assert k1.startswith("swe|")
    assert k1 != k3


def test_jd_fingerprint_ignores_whitespace_only():
    a = resume_store.jd_fingerprint("Python  APIs\n")
    b = resume_store.jd_fingerprint("python apis")
    c = resume_store.jd_fingerprint("python apis and rust")
    assert a == b
    assert a != c


def test_save_and_find_same_posting(tmp_path, monkeypatch):
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
        description="python react apis",
    )
    assert Path(row["pdf_path"]).is_file()
    assert Path(row["tex_path"]).read_text() == tex

    hit = resume_store.find_cached(
        "u1", "Stripe", "Backend Engineer", "swe",
        posting_id=42, description="python react apis",
    )
    assert hit is not None
    assert hit["cache_key"] == row["cache_key"]
    assert resume_store.load_pdf(hit) == pdf


def test_find_does_not_reuse_similar_title_at_same_company(tmp_path, monkeypatch):
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
        description="python backend apis",
    )

    hit = resume_store.find_cached(
        "u1", "Ramp", "Backend Software Engineer", "swe",
        description="python backend apis",
    )
    assert hit is None


def test_find_does_not_reuse_same_title_different_posting(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_TEX_DIR", str(tmp_path))
    from app import config

    config.get_settings.cache_clear()

    resume_store.save(
        "u1",
        "Stripe",
        "Backend Engineer",
        "swe",
        pdf_bytes=b"%PDF-a",
        tex="tex",
        pages=1,
        posting_id=10,
        description="python react apis",
    )

    hit = resume_store.find_cached(
        "u1", "Stripe", "Backend Engineer", "swe",
        posting_id=99, description="python react apis",
    )
    assert hit is None


def test_find_does_not_reuse_same_title_different_jd(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_TEX_DIR", str(tmp_path))
    from app import config

    config.get_settings.cache_clear()

    resume_store.save(
        "u1",
        "Stripe",
        "Backend Engineer",
        "swe",
        pdf_bytes=b"%PDF-py",
        tex="tex",
        pages=1,
        description="python react apis",
    )

    hit = resume_store.find_cached(
        "u1", "Stripe", "Backend Engineer", "swe",
        description="golang distributed systems",
    )
    assert hit is None


def test_find_reuses_exact_title_and_jd_without_posting_id(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_TEX_DIR", str(tmp_path))
    from app import config

    config.get_settings.cache_clear()

    resume_store.save(
        "u1",
        "Stripe",
        "Backend Engineer",
        "swe",
        pdf_bytes=b"%PDF-same",
        tex="tex",
        pages=1,
        description="python react apis",
    )

    hit = resume_store.find_cached(
        "u1", "Stripe", "Backend Engineer", "swe",
        description="python react apis",
    )
    assert hit is not None
    assert resume_store.load_pdf(hit) == b"%PDF-same"


def test_same_posting_retailors_when_jd_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_TEX_DIR", str(tmp_path))
    from app import config

    config.get_settings.cache_clear()

    resume_store.save(
        "u1",
        "Stripe",
        "Backend Engineer",
        "swe",
        pdf_bytes=b"%PDF-old",
        tex="tex",
        pages=1,
        posting_id=42,
        description="python react apis",
    )

    hit = resume_store.find_cached(
        "u1", "Stripe", "Backend Engineer", "swe",
        posting_id=42, description="golang kubernetes",
    )
    assert hit is None


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
        description="pytorch llm",
    )

    assert resume_store.find_cached(
        "u1", "OpenAI", "ML Engineer", "swe",
        description="pytorch llm",
    ) is None


def test_cover_variant_does_not_serve_as_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_TEX_DIR", str(tmp_path))
    from app import config

    config.get_settings.cache_clear()

    resume_store.save(
        "u1", "Stripe", "Backend Engineer", "cover",
        pdf_bytes=b"%PDF-cover",
        tex="letter",
        pages=1,
        posting_id=42,
        description="python react apis",
    )
    resume_store.save(
        "u1", "Stripe", "Backend Engineer", "swe",
        pdf_bytes=b"%PDF-resume",
        tex="resume",
        pages=1,
        posting_id=42,
        description="python react apis",
    )

    cover = resume_store.find_cached(
        "u1", "Stripe", "Backend Engineer", "cover",
        posting_id=42, description="python react apis",
    )
    resume = resume_store.find_cached(
        "u1", "Stripe", "Backend Engineer", "swe",
        posting_id=42, description="python react apis",
    )
    assert cover is not None and cover["variant"] == "cover"
    assert resume is not None and resume["variant"] == "swe"
    assert resume_store.load_pdf(cover) == b"%PDF-cover"
    assert resume_store.load_pdf(resume) == b"%PDF-resume"
