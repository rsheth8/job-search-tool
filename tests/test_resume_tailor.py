"""Resume tailor: variant pick, trim units, compile hook."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app import resume_tailor

REPO_ROOT = Path(__file__).resolve().parents[1]


def _find_tectonic() -> str | None:
    import os

    env = os.environ.get("TECTONIC_BIN", "").strip()
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
        pytest.skip("tectonic not installed")
    return path


def test_pick_variant_ml_title():
    assert resume_tailor.pick_variant("ML Engineer", "python pytorch") == "aiml"


def test_pick_variant_swe_title():
    assert resume_tailor.pick_variant("Backend Software Engineer", "apis") == "swe"


def test_pick_variant_defaults_swe_on_tie():
    assert resume_tailor.pick_variant("Engineer", None) == "swe"


def test_trim_removes_low_relevance_item():
    tex = r"""
\begin{document}
\begin{itemize}
  \item Keep kubernetes and python backend APIs
  \item Cricket photography travel hobbies
\end{itemize}
\end{document}
"""
    trimmed = resume_tailor._trim_least_relevant(
        tex, "Acme", "Backend Engineer", "python kubernetes apis"
    )
    assert "kubernetes" in trimmed
    assert "Cricket" not in trimmed


def test_bases_available(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_TEX_DIR", str(tmp_path))
    from app import config

    config.get_settings.cache_clear()
    assert not resume_tailor.bases_available()
    (tmp_path / "swe.tex").write_text("% swe")
    (tmp_path / "aiml.tex").write_text("% aiml")
    assert resume_tailor.bases_available()


def test_tailor_for_posting_returns_none_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("RESUME_TEX_DIR", str(tmp_path))
    monkeypatch.setenv("RESUME_TAILOR_ENABLED", "false")
    (tmp_path / "swe.tex").write_text("%")
    (tmp_path / "aiml.tex").write_text("%")
    from app import config

    config.get_settings.cache_clear()
    assert resume_tailor.tailor_for_posting("u", "Co", "SWE", None) is None


def test_tailor_for_posting_compiles_minimal_tex(monkeypatch, tmp_path, tectonic_bin):
    """When tectonic is on PATH, a tiny article compiles to a PDF."""
    minimal = r"""
\documentclass{article}
\begin{document}
Hello one page.
\end{document}
"""
    (tmp_path / "swe.tex").write_text(minimal)
    (tmp_path / "aiml.tex").write_text(minimal)
    monkeypatch.setenv("RESUME_TEX_DIR", str(tmp_path))
    monkeypatch.setenv("RESUME_TAILOR_ENABLED", "true")
    monkeypatch.setenv("TECTONIC_BIN", tectonic_bin)
    from app import config

    config.get_settings.cache_clear()

    pdf = resume_tailor._compile_tex(minimal)
    assert pdf is not None
    try:
        assert pdf.read_bytes()[:4] == b"%PDF"
        assert resume_tailor._pdf_page_count(pdf) == 1
    finally:
        pdf.unlink(missing_ok=True)

    monkeypatch.setattr(resume_tailor, "_edit_via_claude", lambda *a, **k: minimal)
    result = resume_tailor.tailor_for_posting("u", "Acme", "Engineer", "hello")
    assert result is not None
    assert result.pdf_bytes[:4] == b"%PDF"
    assert result.pages == 1
