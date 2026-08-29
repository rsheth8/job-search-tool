"""Resume tailor keeps the reference layout, one page, and whole bullets."""
from __future__ import annotations

from app import resume_tailor

REF = r"""
\documentclass[letterpaper,10pt]{article}
\usepackage[margin=0.5in]{geometry}
\begin{document}
\section{EDUCATION}
State University
\section{PROFESSIONAL EXPERIENCE}
\textbf{Intern} \hfill 2024
\begin{itemize}
  \item Built python kubernetes APIs
  \item Cricket photography travel hobbies
\end{itemize}
\section{KEY PROJECTS}
\textbf{App} -- \textit{tool} \hfill 2024
\begin{itemize}
  \item First project bullet about python
  \item Second project bullet about rust
\end{itemize}
\section{SKILLS}
\textbf{Languages:} Python, Go
\end{document}
"""


def test_lock_style_keeps_reference_preamble():
    edited = REF.replace("margin=0.5in", "margin=0.1in").replace(
        "Built python kubernetes APIs", "Shipped python kubernetes APIs"
    )
    locked = resume_tailor.lock_style(REF, edited)
    assert "margin=0.5in" in locked
    assert "margin=0.1in" not in locked
    assert "Shipped python kubernetes APIs" in locked
    assert resume_tailor.section_order(locked) == resume_tailor.section_order(REF)


def test_lock_style_rejects_dropped_section():
    edited = REF.replace(r"\section{EDUCATION}", r"\section{HOBBIES}")
    locked = resume_tailor.lock_style(REF, edited)
    assert r"\section{EDUCATION}" in locked
    assert r"\section{HOBBIES}" not in locked
    assert resume_tailor.section_order(locked) == resume_tailor.section_order(REF)


def test_lock_style_rejects_squeeze_hacks():
    split = resume_tailor._split_tex(REF)
    assert split is not None
    preamble, body = split
    squeezed = preamble + "\n\\enlargethispage{2\\baselineskip}\n" + body + r"\end{document}"
    locked = resume_tailor.lock_style(REF, squeezed)
    assert r"\enlargethispage" not in locked


def test_trim_drops_the_irrelevant_bullet_not_a_section():
    trimmed = resume_tailor._trim_least_relevant(
        REF, "Acme", "Backend Engineer", "python kubernetes apis"
    )
    assert "kubernetes" in trimmed
    assert "Cricket" not in trimmed
    assert resume_tailor.section_order(trimmed) == resume_tailor.section_order(REF)


def test_trim_keeps_a_multiline_item_together():
    tex = r"""
\begin{document}
\begin{itemize}
  \item Keep python kubernetes
  and more words on the next line
  \item Cricket photography travel
\end{itemize}
\end{document}
"""
    trimmed = resume_tailor._trim_least_relevant(
        tex, "Acme", "Backend Engineer", "python kubernetes"
    )
    assert "more words on the next line" in trimmed
    assert "Cricket" not in trimmed


def test_trim_never_empties_a_one_bullet_list():
    tex = r"""
\begin{document}
\section{JOB}
\begin{itemize}
  \item Only bullet about python
\end{itemize}
\end{document}
"""
    trimmed = resume_tailor._trim_least_relevant(
        tex, "Acme", "Engineer", "python"
    )
    assert trimmed == tex
    assert r"\item Only bullet" in trimmed


def test_fit_returns_none_instead_of_clipping_structure(monkeypatch):
    monkeypatch.setattr(resume_tailor, "_compile_one_page", lambda tex: None)
    tex = r"""
\begin{document}
\section{JOB}
\begin{itemize}
  \item Only bullet
\end{itemize}
\end{document}
"""
    assert resume_tailor._fit_one_page(tex, "Acme", "Engineer", "") is None


def test_content_clipped_detects_overfull_vbox():
    assert resume_tailor._content_clipped("Overfull \\vbox (12.0pt too high)")
    assert not resume_tailor._content_clipped("Overfull \\hbox (1.2pt too wide)")


def test_reference_pdf_refuses_multi_page(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUME_TEX_DIR", str(tmp_path))
    from app import config

    config.get_settings.cache_clear()
    (tmp_path / "swe.pdf").write_bytes(b"%PDF-fake")
    monkeypatch.setattr(resume_tailor, "_pdf_page_count", lambda path: 2)
    assert resume_tailor._reference_pdf("swe", "Engineer", "Acme") is None
