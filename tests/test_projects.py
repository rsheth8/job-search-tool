"""Project catalog: score + pick the two that match a posting."""
from __future__ import annotations

from app import projects


def test_catalog_loads_github_projects():
    catalog = projects.load_catalog()
    ids = {p["id"] for p in catalog}
    assert "distill" in ids and "pantrypal" in ids and "lockin" in ids
    assert "songsift" in ids and "mydrive" in ids
    assert len(catalog) >= 15


def test_pick_ios_role_prefers_native_mobile():
    picked = projects.pick(
        "iOS Engineer",
        "SwiftUI HealthKit EventKit native iPhone app",
        "swe",
    )
    names = [p["id"] for p in picked]
    assert "lockin" in names
    assert len(picked) == 2


def test_pick_ml_role_prefers_aiml_projects():
    picked = projects.pick(
        "Machine Learning Engineer",
        "pytorch huggingface transformers llm nlp embeddings",
        "aiml",
    )
    names = [p["id"] for p in picked]
    assert any(n in names for n in ("distill", "movie_sentiment", "songsift", "mydrive"))
    assert len(picked) == 2


def test_pick_java_backend_prefers_spring_rag():
    picked = projects.pick(
        "Backend Software Engineer",
        "Java Spring Boot PostgreSQL Maven REST APIs",
        "swe",
    )
    assert picked[0]["id"] == "textbook_rag"


def test_pick_falls_back_to_two_defaults_without_signal():
    picked = projects.pick("Intern", "", "swe")
    assert len(picked) == 2


def test_inject_replaces_key_projects_section():
    tex = r"""
\section{KEY PROJECTS}
\vspace{-0.6em}\noindent\rule{\linewidth}{0.4pt}\vspace{0.05em}

\textbf{Old} -- \textit{Gone} \hfill \textit{2020}
\begin{itemize}
  \item leftover
\end{itemize}

\section{SKILLS \& AWARDS}
\textbf{Languages:} Python
"""
    distill = projects.by_id("distill")
    assert distill is not None
    out = projects.inject(tex, [distill])
    assert "Distill" in out
    assert "Old" not in out
    assert r"\section{SKILLS" in out
    assert "leftover" not in out


def test_inject_on_base_swe_tex():
    from pathlib import Path

    swe = Path(__file__).resolve().parents[1] / "resumes" / "swe.tex"
    if not swe.is_file():
        return
    tex = swe.read_text(encoding="utf-8")
    picked = projects.pick(
        "ML Engineer", "pytorch llm huggingface transformers", "aiml"
    )
    out = projects.inject(tex, picked)
    assert picked[0]["name"] in out
    assert r"\section{SKILLS" in out
    assert r"\section{PROFESSIONAL EXPERIENCE}" in out
