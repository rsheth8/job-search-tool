"""Integration tests: real resume .tex compiles and stays one page after edits.

Requires Tectonic on PATH (or ``TECTONIC_BIN`` / ``.cache/tectonic/tectonic``)
and the base ``resumes/swe.tex`` + ``resumes/aiml.tex`` files.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app import resume_tailor

REPO_ROOT = Path(__file__).resolve().parents[1]
RESUMES_DIR = REPO_ROOT / "resumes"

# Simulates a moderate Claude edit — rewording, no new bullets.
MINIMAL_EDIT = lambda tex: tex.replace(  # noqa: E731
    "React Native",
    "React Native and TypeScript",
    1,
).replace(
    r"\textbf{Software Developer Intern}",
    r"\textbf{Software Developer Intern}",
)  # no-op on aiml variant for second replace

MINIMAL_EDIT_AIML = lambda tex: tex.replace(  # noqa: E731
    "PyTorch",
    "PyTorch and scikit-learn",
    1,
)

# One extra bullet is enough to spill this template to a second page.
OVERFLOW_BULLET = (
    r"  \item Partnered with cross-functional teams to deliver incremental "
    r"product improvements"
)


def _find_tectonic() -> str | None:
    env = __import__("os").environ.get("TECTONIC_BIN", "").strip()
    if env and Path(env).is_file():
        return str(Path(env).resolve())
    found = shutil.which("tectonic")
    if found:
        return found
    cached = REPO_ROOT / ".cache" / "tectonic" / "tectonic"
    if cached.is_file():
        return str(cached.resolve())
    return None


@pytest.fixture
def tectonic_bin() -> str:
    path = _find_tectonic()
    if not path:
        pytest.skip("tectonic not installed (set TECTONIC_BIN or add to PATH)")
    return path


@pytest.fixture
def real_resumes(monkeypatch, tectonic_bin):
    """Point settings at repo resumes/ and enable tailoring."""
    if not (
        (RESUMES_DIR / "swe.tex").is_file() and (RESUMES_DIR / "aiml.tex").is_file()
    ):
        pytest.skip("resumes/swe.tex and resumes/aiml.tex not present")

    monkeypatch.setenv("RESUME_TEX_DIR", str(RESUMES_DIR))
    monkeypatch.setenv("RESUME_TAILOR_ENABLED", "true")
    monkeypatch.setenv("TECTONIC_BIN", tectonic_bin)
    from app import config

    config.get_settings.cache_clear()
    yield RESUMES_DIR


def _compile_pages(tex: str) -> int:
    pdf = resume_tailor._compile_tex(tex)
    assert pdf is not None, "tectonic compile failed"
    try:
        return resume_tailor._pdf_page_count(pdf)
    finally:
        pdf.unlink(missing_ok=True)


def _read_base(variant: str) -> str:
    return (RESUMES_DIR / f"{variant}.tex").read_text(encoding="utf-8")


def _add_overflow_bullet(tex: str) -> str:
    return tex.replace(
        r"\begin{itemize}",
        r"\begin{itemize}" + "\n" + OVERFLOW_BULLET,
        1,
    )


@pytest.mark.parametrize("variant", ["swe", "aiml"])
def test_base_resume_compiles_to_one_page(real_resumes, variant):
    assert (RESUMES_DIR / f"{variant}.tex").is_file()
    pages = _compile_pages(_read_base(variant))
    assert pages == 1, f"{variant}.tex base should be exactly one page, got {pages}"


def test_reference_pdfs_are_one_page(real_resumes):
    from pypdf import PdfReader

    pdfs = list(RESUMES_DIR.glob("*.pdf"))
    assert pdfs, "expected reference PDFs in resumes/"
    for pdf_path in pdfs:
        pages = len(PdfReader(str(pdf_path)).pages)
        assert pages == 1, f"{pdf_path.name} should be one page, got {pages}"


@pytest.mark.parametrize(
    ("variant", "editor"),
    [
        ("swe", MINIMAL_EDIT),
        ("aiml", MINIMAL_EDIT_AIML),
    ],
)
def test_minimal_edit_stays_one_page_without_trim(real_resumes, variant, editor):
    edited = editor(_read_base(variant))
    pages = _compile_pages(edited)
    assert pages == 1


@pytest.mark.parametrize("variant", ["swe", "aiml"])
def test_minimal_edit_through_fit_one_page(real_resumes, variant):
    editor = MINIMAL_EDIT if variant == "swe" else MINIMAL_EDIT_AIML
    edited = editor(_read_base(variant))
    fitted = resume_tailor._fit_one_page(
        edited,
        "Acme",
        "Software Engineer",
        "python react apis backend",
    )
    assert _compile_pages(fitted) == 1


@pytest.mark.parametrize("variant", ["swe", "aiml"])
def test_overflow_edit_is_trimmed_to_one_page(real_resumes, variant):
    bloated = _add_overflow_bullet(_read_base(variant))
    assert _compile_pages(bloated) >= 2, "overflow fixture should spill to page 2"

    fitted = resume_tailor._fit_one_page(
        bloated,
        "Stripe",
        "Backend Software Engineer",
        "python react kubernetes apis spring boot",
    )
    assert _compile_pages(fitted) == 1
    assert r"\begin{document}" in fitted
    assert r"\end{document}" in fitted
    # Trim should drop the synthetic bullet, not gut the whole resume.
    assert r"\section{PROFESSIONAL EXPERIENCE}" in fitted


def test_tailor_for_posting_one_page_after_minimal_edit(real_resumes, monkeypatch):
    monkeypatch.setattr(
        resume_tailor,
        "_edit_via_claude",
        lambda base, company, title, desc: MINIMAL_EDIT(base),
    )
    result = resume_tailor.tailor_for_posting(
        "u1",
        "Stripe",
        "Backend Software Engineer",
        "python react node apis kubernetes",
    )
    assert result is not None
    assert result.pages == 1
    assert result.pdf_bytes[:4] == b"%PDF"
    assert result.variant == "swe"
    assert not result.from_cache


def test_tailor_for_posting_trims_after_overflow_edit(real_resumes, monkeypatch):
    monkeypatch.setattr(
        resume_tailor,
        "_edit_via_claude",
        lambda base, company, title, desc: _add_overflow_bullet(base),
    )
    result = resume_tailor.tailor_for_posting(
        "u1",
        "OpenAI",
        "ML Engineer",
        "pytorch llm machine learning python",
    )
    assert result is not None
    assert result.pages == 1, "tailor_for_posting must enforce the one-page limit"
    assert len(result.pdf_bytes) > 1000


def test_tailor_reuses_cached_resume_without_reediting(real_resumes, monkeypatch):
    edits = {"n": 0}

    def counting_edit(base, company, title, desc):
        edits["n"] += 1
        return MINIMAL_EDIT(base)

    monkeypatch.setattr(resume_tailor, "_edit_via_claude", counting_edit)

    first = resume_tailor.tailor_for_posting(
        "u1", "Stripe", "Backend Engineer", "python react apis", posting_id=10
    )
    assert first is not None and not first.from_cache
    assert edits["n"] == 1

    second = resume_tailor.tailor_for_posting(
        "u1", "Stripe", "Backend Engineer", "python react apis", posting_id=99
    )
    assert second is not None and second.from_cache
    assert second.pages == 1
    assert second.pdf_bytes == first.pdf_bytes
    assert edits["n"] == 1, "cache hit should skip Claude + recompile"


def test_picked_variant_matches_posting(real_resumes, monkeypatch):
    monkeypatch.setattr(
        resume_tailor,
        "_edit_via_claude",
        lambda base, company, title, desc: base,
    )
    ml = resume_tailor.tailor_for_posting(
        "u1", "Anthropic", "Machine Learning Engineer", "pytorch transformers llm"
    )
    swe = resume_tailor.tailor_for_posting(
        "u1", "Stripe", "Backend Software Engineer", "distributed systems golang apis"
    )
    assert ml is not None and ml.variant == "aiml" and ml.pages == 1
    assert swe is not None and swe.variant == "swe" and swe.pages == 1
