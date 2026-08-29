"""Optional one-page cover letter: layout we own, facts only, cache isolation."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from app import applicant, apply_queue, coverletter, jobstore, knowledge, resume_store
from app.jobsources import JobPosting


def _blank_pdf(path: Path) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with path.open("wb") as fh:
        writer.write(fh)


def _enable(monkeypatch, tmp_path):
    monkeypatch.setenv("COVER_LETTER_ENABLED", "true")
    monkeypatch.setenv("RESUME_TEX_DIR", str(tmp_path))
    from app import config

    config.get_settings.cache_clear()


def _fake_compile(tmp_path):
    def compile_one_page(tex: str):
        fd, dest = tempfile.mkstemp(suffix=".pdf", prefix="cover_")
        os.close(fd)
        path = Path(dest)
        _blank_pdf(path)
        return path, 1, tex

    return compile_one_page


def _ident(**extra):
    applicant.set_identity("u1", {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "phone": "555-0100",
        "city": "Chicago",
        "state": "IL",
        "linkedin": "https://www.linkedin.com/in/ada",
        **extra,
    })


def _posting(**kw) -> dict:
    return {
        "id": kw.get("id", 7),
        "company": kw.get("company", "Stripe"),
        "title": kw.get("title", "Backend Engineer"),
        "description": kw.get(
            "description",
            "You will own payment APIs and keep p99 latency low. Python and Go.",
        ),
    }


def test_disabled_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("COVER_LETTER_ENABLED", "false")
    monkeypatch.setenv("RESUME_TEX_DIR", str(tmp_path))
    from app import config

    config.get_settings.cache_clear()
    assert coverletter.for_posting("u1", _posting()) is None


def test_template_is_a_real_letter_not_a_form_letter():
    _ident(current_title="Software intern", current_company="Acme")
    knowledge.add("u1", "project", "Built a real-time pricing service in Go")
    copy = coverletter._template_copy(
        "Stripe", "Backend Engineer",
        "You will own payment APIs and keep p99 latency low.",
        applicant.get_identity("u1"),
        knowledge.knowledge_block("u1", context="Backend Stripe APIs"),
    )
    assert copy.greeting == "Dear Stripe Hiring Team,"
    assert copy.closing == "Sincerely,"
    assert len(copy.paragraphs) == 3
    joined = " ".join(copy.paragraphs).lower()
    assert "to whom it may concern" not in joined
    assert "i am writing to apply" not in joined
    assert "i am excited to apply" not in joined
    assert "i am the perfect candidate" not in joined
    assert "pricing service" in joined
    assert "backend engineer" in joined


def test_generic_jd_opener_is_skipped():
    copy = coverletter._template_copy(
        "Ramp", "Software Engineer",
        "We are looking for a Software Engineer to join our team. "
        "Ramp is building corporate cards for startups.",
        {},
        "",
    )
    assert "we are looking for" not in copy.paragraphs[0].lower()
    assert "corporate cards" in copy.paragraphs[0].lower()


def test_render_tex_is_block_business_letter():
    _ident()
    copy = coverletter.LetterCopy(
        greeting="Dear Stripe Hiring Team,",
        paragraphs=["First paragraph.", "Second paragraph.", "Third paragraph."],
        closing="Sincerely,",
    )
    tex = coverletter.render_tex(
        applicant.get_identity("u1"), "Stripe", "Backend Engineer", copy,
    )
    assert r"\documentclass[11pt,letterpaper]{article}" in tex
    assert "Ada Lovelace" in tex
    assert "ada@example.com" in tex
    assert "linkedin.com/in/ada" in tex
    assert r"\today" in tex
    assert "Hiring Team" in tex
    assert r"\textit{Re: Backend Engineer}" in tex
    assert "Dear Stripe Hiring Team," in tex
    assert "First paragraph." in tex
    assert "Sincerely," in tex
    assert "Enclosure: Resume" in tex
    assert r"\pagestyle{empty}" in tex
    assert r"\parindent}{0pt}" in tex


def test_tex_escapes_specials():
    ident = {"full_name": "Ann_Lee", "email": "ann_lee@ex.com"}
    copy = coverletter.LetterCopy(
        greeting="Dear Hiring Team,",
        paragraphs=["I used C++ & Python at 100% capacity."],
        closing="Sincerely,",
    )
    tex = coverletter.render_tex(ident, "Johnson & Johnson", "SWE", copy)
    assert r"Ann\_Lee" in tex
    assert r"Johnson \& Johnson" in tex
    assert r"C++ \& Python at 100\% capacity." in tex
    assert "Johnson & Johnson" not in tex  # raw ampersand would break TeX


def test_weak_opener_is_rewritten():
    opening = "I am writing to apply for this role at Stripe."
    assert coverletter._WEAK_OPEN.search(opening)
    rewritten = coverletter._opening(
        "Stripe", "Backend Engineer", "Own our payment APIs.",
    )
    assert not coverletter._WEAK_OPEN.search(rewritten)


def test_sqlite_row_does_not_need_dict_get(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    _ident()
    monkeypatch.setattr("app.resume_tailor._compile_one_page", _fake_compile(tmp_path))
    row = jobstore.save_posting(
        "u1",
        JobPosting("greenhouse", "cl1", "Backend Engineer", "https://x/1",
                   company="Stripe", description="Own payment APIs in Python."),
        relevance_score=0.8, status="queued",
    )
    result = coverletter.for_posting("u1", row)
    assert result is not None
    assert result.pages == 1
    assert result.filename.startswith("Cover_Letter_")
    assert result.pdf_bytes[:4] == b"%PDF"


def test_same_posting_reuses_cover_not_resume(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    _ident()
    monkeypatch.setattr("app.resume_tailor._compile_one_page", _fake_compile(tmp_path))
    posting = _posting(id=42, description="python react apis")
    first = coverletter.for_posting("u1", posting)
    assert first is not None and not first.from_cache

    resume_store.save(
        "u1", "Stripe", "Backend Engineer", "swe",
        pdf_bytes=first.pdf_bytes, tex="resume-tex", pages=1,
        posting_id=42, description="python react apis",
    )
    cover_hit = resume_store.find_cached(
        "u1", "Stripe", "Backend Engineer", "cover",
        posting_id=42, description="python react apis",
    )
    resume_hit = resume_store.find_cached(
        "u1", "Stripe", "Backend Engineer", "swe",
        posting_id=42, description="python react apis",
    )
    assert cover_hit is not None
    assert resume_hit is not None
    assert cover_hit["variant"] == "cover"
    assert resume_hit["variant"] == "swe"

    second = coverletter.for_posting("u1", posting)
    assert second is not None and second.from_cache


def test_package_does_not_build_a_cover(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(
        "app.coverletter.for_posting",
        lambda *a, **k: called.append(True) or None,
    )
    row = jobstore.save_posting(
        "u1",
        JobPosting("greenhouse", "cl2", "Role", "https://x/2", company="Acme"),
        relevance_score=0.7, status="queued",
    )
    apply_queue.stage("u1", row["id"])
    pkg = apply_queue.get_package("u1", row["id"])
    assert pkg is not None
    assert "cover" not in pkg
    assert called == []


def test_compile_template_is_one_page(monkeypatch, tmp_path):
    import shutil

    from app import resume_tailor

    if not resume_tailor.resolve_tectonic() and not shutil.which("tectonic"):
        import pytest

        pytest.skip("tectonic not installed")
    _enable(monkeypatch, tmp_path)
    _ident()
    copy = coverletter._template_copy(
        "Stripe", "Backend Engineer",
        "You will own payment APIs.",
        applicant.get_identity("u1"),
        "",
    )
    tex = coverletter.render_tex(
        applicant.get_identity("u1"), "Stripe", "Backend Engineer", copy,
    )
    compiled = resume_tailor._compile_one_page(tex)
    assert compiled is not None
    pdf, pages, _ = compiled
    try:
        assert pages == 1
    finally:
        pdf.unlink(missing_ok=True)
