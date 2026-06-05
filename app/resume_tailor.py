"""Tailor a base resume (.tex) to a job posting.

Flow:
  1. Check the cache — reuse a stored one-page PDF when it fits.
  2. Pick SWE vs AI/ML base from the posting title/description.
  3. Ask Claude for moderate, truthful edits to the LaTeX body.
  4. Compile with Tectonic; trim and recompile until exactly one page.
  5. Persist PDF + .tex on the volume for future applies.

Base .tex files live on the Fly volume (``RESUME_TEX_DIR``), not in git.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import resume_store
from .config import get_settings

logger = logging.getLogger("resume_tailor")

_VARIANTS = ("swe", "aiml")
_MAX_TRIM_ROUNDS = 40

# Whole ``\\item ...`` lines are the safest trim unit for this template.
_ITEM_RE = re.compile(r"(\\item[^\n]*)", re.MULTILINE)

# Project blocks: bold title line + following itemize environment.
_PROJECT_RE = re.compile(
    r"(\\textbf\{[^}]+\}[^\n]*\\hfill[^\n]*\n\\begin\{itemize\}.*?\n\\end\{itemize\})",
    re.DOTALL,
)

# Skills / education lines like ``\\textbf{Languages:} ...``
_SKILLS_LINE_RE = re.compile(r"(\\textbf\{[^}]+\}:[^\n]*)", re.MULTILINE)

# Optional sections trimmed only after bullets/projects/skills are exhausted.
_OPTIONAL_LINE_RES = (
    re.compile(r"(\\textbf\{Personal Interests\}:.*)", re.MULTILINE),
    re.compile(r"(\\textbf\{Interests\}:.*)", re.MULTILINE),
    re.compile(r"(\\textbf\{Events\}:.*)", re.MULTILINE),
    re.compile(r"(\\textbf\{Honors\}:.*)", re.MULTILINE),
    re.compile(r"(\\textbf\{Leadership\}:.*)", re.MULTILINE),
)

_ML_SIGNALS = (
    "machine learning", "ml engineer", "ai engineer", "data scientist",
    "research scientist", "deep learning", "nlp", "computer vision", "llm",
    "pytorch", "tensorflow", "generative ai", "applied scientist", "mle",
)
_SWE_SIGNALS = (
    "software engineer", "swe", "backend", "frontend", "full stack",
    "full-stack", "platform engineer", "site reliability", "sre",
    "mobile engineer", "ios engineer", "android engineer", "web developer",
    "staff engineer", "infra engineer",
)


@dataclass(frozen=True)
class TailorResult:
    pdf_bytes: bytes
    filename: str
    variant: str
    pages: int
    from_cache: bool = False


def resume_dir() -> Path:
    return Path(get_settings().resume_tex_dir)


def bases_available() -> bool:
    d = resume_dir()
    return all((d / f"{v}.tex").is_file() for v in _VARIANTS)


def pick_variant(title: str, description: str | None) -> str:
    """Heuristic SWE vs AI/ML pick from posting text."""
    text = f"{title or ''} {(description or '')}".lower()
    ml = sum(1 for k in _ML_SIGNALS if k in text)
    swe = sum(1 for k in _SWE_SIGNALS if k in text)
    if ml > swe:
        return "aiml"
    if swe > ml:
        return "swe"
    t = (title or "").lower()
    if any(w in t for w in ("ml", "ai", "machine learning", "data")):
        return "aiml"
    return "swe"


def tailor_for_posting(
    user_id: str,
    company: str,
    title: str,
    description: str | None,
    *,
    posting_id: int | None = None,
) -> TailorResult | None:
    """Return a one-page tailored PDF, or None if tailoring is unavailable."""
    s = get_settings()
    if not s.resume_tailor_enabled or not bases_available():
        return None

    variant = pick_variant(title, description)

    cached = resume_store.find_cached(
        user_id, company, title, variant, posting_id=posting_id
    )
    if cached is not None:
        hit = _from_cache_row(cached)
        if hit is not None:
            return hit

    base_tex = (resume_dir() / f"{variant}.tex").read_text(encoding="utf-8")
    edited = _edit_via_claude(base_tex, company, title, description)
    final_tex = _fit_one_page(edited, company, title, description)
    built = _compile_one_page(final_tex)
    if built is None:
        logger.error(
            "could not produce a one-page resume for %s @ %s", title, company
        )
        return None

    pdf_path, pages, final_tex = built
    try:
        pdf_bytes = pdf_path.read_bytes()
    finally:
        pdf_path.unlink(missing_ok=True)

    resume_store.save(
        user_id,
        company,
        title,
        variant,
        pdf_bytes=pdf_bytes,
        tex=final_tex,
        pages=pages,
        posting_id=posting_id,
    )

    return TailorResult(
        pdf_bytes=pdf_bytes,
        filename=_filename(title, company),
        variant=variant,
        pages=pages,
        from_cache=False,
    )


def _from_cache_row(row) -> TailorResult | None:
    pdf_path = Path(row["pdf_path"])
    if not pdf_path.is_file():
        return None
    pages = _pdf_page_count(pdf_path)
    if pages != 1:
        logger.warning("cached resume %s is %s pages; skipping", row["id"], pages)
        return None
    return TailorResult(
        pdf_bytes=pdf_path.read_bytes(),
        filename=_filename(row["title"] or "", row["company"] or ""),
        variant=row["variant"],
        pages=1,
        from_cache=True,
    )


def _filename(title: str, company: str) -> str:
    return f"Resume_{_slug(title or 'resume')}_{_slug(company or 'company')}.pdf"


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip()).strip("_")
    return cleaned[:40] or "resume"


def _edit_via_claude(
    base_tex: str, company: str, title: str, description: str | None
) -> str:
    """Moderate truthful edits; fall back to the base on any failure."""
    if not get_settings().use_llm_router:
        return base_tex
    try:
        import anthropic

        s = get_settings()
        client = anthropic.Anthropic(api_key=s.anthropic_api_key)
        desc = (description or "").strip()[:2000]
        resp = client.messages.create(
            model=s.anthropic_model,
            max_tokens=4096,
            system=(
                "You edit LaTeX resumes for job applications. Rules:\n"
                "- Output ONLY the complete .tex file, no markdown fences.\n"
                "- Keep the same document class, packages, layout, and section "
                "structure — do NOT switch templates.\n"
                "- Make moderate, truthful edits: reorder bullets, emphasize "
                "relevant experience/skills, tighten wording. Never invent "
                "employers, degrees, metrics, or tools the candidate didn't list.\n"
                "- Prefer rewording over adding bullets — the resume MUST stay "
                "one page.\n"
                "- Do not remove entire jobs or projects unless clearly irrelevant.\n"
                "- Preserve LaTeX syntax exactly (\\\\item, \\\\textbf{}, etc.)."
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"Target role: {title} at {company}.\n"
                    f"Job description (may be truncated):\n{desc or '(not provided)'}\n\n"
                    f"Base resume:\n{base_tex}"
                ),
            }],
        )
        text = next((b.text for b in resp.content if b.type == "text"), "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:latex|tex)?\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
        if "\\begin{document}" in text and "\\end{document}" in text:
            return text
    except Exception:
        logger.exception("Claude resume edit failed; using base .tex")
    return base_tex


def _fit_one_page(
    tex: str, company: str, title: str, description: str | None
) -> str:
    """Compile and trim until the PDF is exactly one page."""
    working = tex
    phase = 0  # 0=bullets/projects/skills, 1=optional lines

    for _ in range(_MAX_TRIM_ROUNDS):
        compiled = _compile_one_page(working)
        if compiled is not None:
            pdf_path, _, verified_tex = compiled
            pdf_path.unlink(missing_ok=True)
            return verified_tex

        trimmed = _trim_least_relevant(
            working, company, title, description, allow_optional=(phase >= 1)
        )
        if trimmed == working:
            if phase == 0:
                phase = 1
                continue
            logger.warning("resume still >1 page but no trim targets left")
            return working
        working = trimmed

    return working


def _compile_one_page(tex: str) -> tuple[Path, int, str] | None:
    """Compile ``tex``; return (pdf_path, 1, tex) only when the PDF is one page."""
    pdf = _compile_tex(tex)
    if pdf is None:
        return None
    try:
        pages = _pdf_page_count(pdf)
        if pages == 1:
            return pdf, 1, tex
        pdf.unlink(missing_ok=True)
        return None
    except Exception:
        pdf.unlink(missing_ok=True)
        raise


def _trim_least_relevant(
    tex: str,
    company: str,
    title: str,
    description: str | None,
    *,
    allow_optional: bool = False,
) -> str:
    """Remove one whole bullet, project block, skills line, or optional row."""
    context = f"{title} {company} {description or ''}".lower()
    items = _ITEM_RE.findall(tex)
    if items:
        worst = min(items, key=lambda block: _relevance(block, context))
        return _cleanup_empty_itemize(tex.replace(worst, "", 1))

    projects = _PROJECT_RE.findall(tex)
    if projects:
        worst = min(projects, key=lambda block: _relevance(block, context))
        return tex.replace(worst, "", 1)

    skills_lines = _SKILLS_LINE_RE.findall(tex)
    droppable = [
        ln for ln in skills_lines
        if not ln.lower().startswith("\\textbf{languages:")
    ]
    if droppable:
        worst = min(droppable, key=lambda block: _relevance(block, context))
        return tex.replace(worst, "", 1)

    if allow_optional:
        for pattern in _OPTIONAL_LINE_RES:
            match = pattern.search(tex)
            if match:
                return tex.replace(match.group(1), "", 1)

    return tex


def _cleanup_empty_itemize(tex: str) -> str:
    """Remove itemize environments that lost all items."""
    return re.sub(
        r"\\begin\{itemize\}\s*\\end\{itemize\}",
        "",
        tex,
        flags=re.MULTILINE,
    )


def _relevance(block: str, context: str) -> float:
    words = set(re.findall(r"[a-z0-9+#.]{3,}", block.lower()))
    ctx = set(re.findall(r"[a-z0-9+#.]{3,}", context))
    if not words:
        return 0.0
    return len(words & ctx) / len(words)


def _tectonic_safe(tex: str) -> str:
    return tex.replace(
        r"\usepackage[protrusion,expansion]{microtype}",
        r"\usepackage[protrusion=true,expansion=false]{microtype}",
    )


def _compile_tex(tex: str) -> Path | None:
    s = get_settings()
    tectonic = s.tectonic_bin
    with tempfile.TemporaryDirectory(prefix="resume_") as tmp:
        work = Path(tmp)
        tex_path = work / "resume.tex"
        tex_path.write_text(_tectonic_safe(tex), encoding="utf-8")
        out_dir = work / "out"
        out_dir.mkdir()
        try:
            subprocess.run(
                [tectonic, "--outdir", str(out_dir), str(tex_path)],
                check=True,
                capture_output=True,
                timeout=120,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            logger.exception("tectonic compile failed")
            return None
        built = out_dir / "resume.pdf"
        if not built.is_file():
            return None
        fd, dest = tempfile.mkstemp(suffix=".pdf", prefix="resume_")
        os.close(fd)
        dest_path = Path(dest)
        shutil.copy(built, dest_path)
        return dest_path


def _pdf_page_count(path: Path) -> int:
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:
        logger.exception("pypdf page count failed; falling back to byte scan")
        data = path.read_bytes()
        pages = re.findall(rb"/Type\s*/Page\b", data)
        return len(pages) if pages else 1
