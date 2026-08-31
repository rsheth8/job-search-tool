"""Tailor a base resume (.tex) to a job posting.

Flow:
  1. Check the cache — reuse a stored one-page PDF when it fits.
  2. Pick SWE vs AI/ML base from the posting title/description.
  3. Ask Claude for moderate, truthful edits to the LaTeX *body*.
  4. Lock the reference preamble/section structure (never a new template).
  5. Compile with Tectonic; trim whole bullets until exactly one page,
     with no overflow off the bottom.
  6. Persist PDF + .tex only when that one-page check passes.

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

_BEGIN_DOC_RE = re.compile(r"\\begin\{document\}")
_SECTION_RE = re.compile(r"\\section\*?\{([^}]+)\}")
_ITEMIZE_RE = re.compile(r"\\begin\{itemize\}(.*?)\\end\{itemize\}", re.DOTALL)
_ITEM_SPLIT_RE = re.compile(r"(?m)(?=^[ \t]*\\item\b)")
_CLIPPED_RE = re.compile(r"Overfull \\vbox", re.I)
_SQUEEZE_RE = re.compile(
    r"\\(?:enlargethispage|newpage|pagebreak|newgeometry|"
    r"vspace\*?\s*\{-\s*\d|vskip\s*-)",
    re.I,
)
_LAYOUT_MARKERS = (
    r"\documentclass",
    r"\usepackage",
    r"\geometry",
    r"\setmainfont",
    r"\setmathfont",
    r"\pagestyle",
)

# Optional rows we may drop after extra bullets are gone — not core sections.
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
        user_id, company, title, variant,
        posting_id=posting_id, description=description,
    )
    if cached is not None:
        hit = _from_cache_row(cached)
        if hit is not None:
            return hit

    base_tex = (resume_dir() / f"{variant}.tex").read_text(encoding="utf-8")
    from . import projects as project_catalog

    chosen = project_catalog.pick(title, description, variant)
    if chosen:
        base_tex = project_catalog.inject(base_tex, chosen)
    edited = lock_style(base_tex, _edit_via_claude(base_tex, company, title, description))
    fitted = _fit_one_page(edited, company, title, description)
    if fitted is None:
        fitted = _fit_one_page(base_tex, company, title, description)
    built = _compile_one_page(fitted) if fitted is not None else None
    if built is None:
        return _reference_pdf(variant, title, company)

    pdf_path, pages, final_tex = built
    try:
        pdf_bytes = pdf_path.read_bytes()
    finally:
        pdf_path.unlink(missing_ok=True)

    if pages != 1:
        logger.error("refusing to save a %s-page resume for %s @ %s", pages, title, company)
        return _reference_pdf(variant, title, company)

    resume_store.save(
        user_id,
        company,
        title,
        variant,
        pdf_bytes=pdf_bytes,
        tex=final_tex,
        pages=pages,
        posting_id=posting_id,
        description=description,
    )

    return TailorResult(
        pdf_bytes=pdf_bytes,
        filename=_filename(title, company),
        variant=variant,
        pages=1,
        from_cache=False,
    )


def _reference_pdf(variant: str, title: str, company: str) -> TailorResult | None:
    """Last resort: the untouched one-page reference PDF, never a clipped page."""
    base_pdf = resume_dir() / f"{variant}.pdf"
    if not base_pdf.is_file():
        logger.error("could not produce a one-page resume for %s @ %s", title, company)
        return None
    pages = _pdf_page_count(base_pdf)
    if pages != 1:
        logger.error("reference %s.pdf is %s pages; not serving it", variant, pages)
        return None
    logger.warning(
        "tailored compile could not stay one page for %s @ %s; using base %s.pdf",
        title, company, variant,
    )
    return TailorResult(
        pdf_bytes=base_pdf.read_bytes(),
        filename=_filename(title, company),
        variant=variant,
        pages=1,
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


def section_order(tex: str) -> list[str]:
    return _SECTION_RE.findall(tex or "")


def lock_style(base: str, edited: str) -> str:
    """Keep the reference preamble and section list. Only the body text may change."""
    split_base = _split_tex(base)
    split_edit = _split_tex(edited)
    if split_base is None:
        return base
    if split_edit is None:
        logger.info("resume edit was not a complete .tex; using reference")
        return base
    preamble, base_body = split_base
    _, edit_body = split_edit
    if section_order(base_body) != section_order(edit_body):
        logger.info("resume edit changed section structure; using reference")
        return base
    if _introduces_layout_change(base_body, edit_body):
        logger.info("resume edit changed layout commands; using reference")
        return base
    return preamble + edit_body + "\n\\end{document}\n"


def _split_tex(tex: str) -> tuple[str, str] | None:
    """(preamble including \\begin{document}, body before \\end{document})."""
    m = _BEGIN_DOC_RE.search(tex or "")
    if not m:
        return None
    end = (tex or "").rfind(r"\end{document}")
    if end < 0 or end < m.end():
        return None
    return tex[: m.end()], tex[m.end(): end]


def _introduces_layout_change(base_body: str, edit_body: str) -> bool:
    for marker in _LAYOUT_MARKERS:
        if marker in edit_body and marker not in base_body:
            return True
    if _SQUEEZE_RE.search(edit_body) and not _SQUEEZE_RE.search(base_body):
        return True
    return False


def _edit_via_claude(
    base_tex: str, company: str, title: str, description: str | None
) -> str:
    """Moderate truthful edits; fall back to the base on any failure."""
    if not get_settings().use_llm_router:
        return base_tex
    try:
        from . import llm_health

        from . import llm_budget

        if not llm_budget.consume(feature="draft"):
            return base_tex

        s = get_settings()
        client = llm_health.client(s.anthropic_api_key)
        desc = (description or "").strip()[:2000]
        resp = client.messages.create(
            model=s.anthropic_model,
            max_tokens=4096,
            system=(
                "You edit LaTeX resumes for job applications. Rules:\n"
                "- Output ONLY the complete .tex file, no markdown fences.\n"
                "- Keep the SAME document class, packages, geometry, fonts, "
                "lengths, and section headings — do NOT switch templates, add "
                "packages, change margins, or squeeze with negative vspace, "
                "\\enlargethispage, \\newpage, or \\tiny.\n"
                "- Keep every \\section that is already there, in the same order.\n"
                "- Make moderate, truthful edits: reorder bullets, emphasize "
                "relevant experience/skills, tighten wording. Never invent "
                "employers, degrees, metrics, or tools the candidate didn't list.\n"
                "- Prefer rewording over adding bullets — the resume MUST stay "
                "one page with nothing clipped off the bottom.\n"
                "- KEY PROJECTS were already chosen for THIS posting. Keep those "
                "two projects. Reorder or tighten bullets; do not swap in other "
                "work or invent projects.\n"
                "- Do not remove entire jobs or sections.\n"
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
) -> str | None:
    """Trim whole bullets until the PDF is exactly one page with no overflow.

    Returns None instead of a clipped or multi-page file.
    """
    working = tex
    phase = 0  # 0=extra bullets, 1=optional interest/honors lines

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
            logger.warning("resume still >1 page; refusing to clip structure")
            return None
        if section_order(trimmed) != section_order(tex):
            logger.warning("trim would drop a section; stopping")
            return None
        working = trimmed

    logger.warning("resume still >1 page after %s trim rounds", _MAX_TRIM_ROUNDS)
    return None


def _compile_one_page(tex: str) -> tuple[Path, int, str] | None:
    """Compile ``tex``; return (pdf_path, 1, tex) only for a clean one-page PDF."""
    if not tex:
        return None
    ran = _run_tectonic(tex)
    if ran is None:
        return None
    pdf, log = ran
    try:
        pages = _pdf_page_count(pdf)
        if pages != 1 or _content_clipped(log):
            pdf.unlink(missing_ok=True)
            return None
        return pdf, 1, tex
    except Exception:
        pdf.unlink(missing_ok=True)
        raise


def _content_clipped(log: str) -> bool:
    """True when LaTeX overflowed the page (text would be cut off)."""
    return bool(_CLIPPED_RE.search(log or ""))


def _trim_least_relevant(
    tex: str,
    company: str,
    title: str,
    description: str | None,
    *,
    allow_optional: bool = False,
) -> str:
    """Remove one whole extra bullet (never the last in a list) or an optional row."""
    context = f"{title} {company} {description or ''}".lower()
    extras = _extra_items(tex)
    if extras:
        worst = min(extras, key=lambda block: _relevance(block, context))
        return _cleanup_empty_itemize(tex.replace(worst, "", 1))

    if allow_optional:
        for pattern in _OPTIONAL_LINE_RES:
            match = pattern.search(tex)
            if match:
                return tex.replace(match.group(1), "", 1)

    return tex


def _extra_items(tex: str) -> list[str]:
    """``\\item`` blocks from lists that still have more than one bullet.

    Never returns the last remaining bullet in a job or project, so a header
    is never left sitting over an empty list (that looks cut off).
    """
    extras: list[str] = []
    for inner in _ITEMIZE_RE.findall(tex):
        items = _items_in(inner)
        if len(items) > 1:
            extras.extend(items)
    return extras


def _items_in(inner: str) -> list[str]:
    chunks = _ITEM_SPLIT_RE.split(inner)
    return [c for c in chunks if re.match(r"[ \t]*\\item\b", c or "")]


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


def resolve_tectonic() -> str | None:
    """Locate the tectonic binary: settings path, $PATH, then repo .cache."""
    s = get_settings()
    configured = (s.tectonic_bin or "").strip()
    if configured and Path(configured).is_file():
        return str(Path(configured).resolve())
    if configured and configured != "tectonic":
        logger.warning("TECTONIC_BIN=%s not found; trying PATH/cache", configured)
    found = shutil.which(configured or "tectonic")
    if found:
        return found
    cached = Path(__file__).resolve().parents[1] / ".cache" / "tectonic" / "tectonic"
    if cached.is_file():
        return str(cached.resolve())
    return None


def _compile_tex(tex: str) -> Path | None:
    ran = _run_tectonic(tex)
    return None if ran is None else ran[0]


def _run_tectonic(tex: str) -> tuple[Path, str] | None:
    tectonic = resolve_tectonic()
    if not tectonic:
        logger.error(
            "tectonic not found — set TECTONIC_BIN or install tectonic "
            "(repo ships .cache/tectonic/tectonic)"
        )
        return None
    with tempfile.TemporaryDirectory(prefix="resume_") as tmp:
        work = Path(tmp)
        tex_path = work / "resume.tex"
        tex_path.write_text(_tectonic_safe(tex), encoding="utf-8")
        out_dir = work / "out"
        out_dir.mkdir()
        try:
            proc = subprocess.run(
                [tectonic, "--outdir", str(out_dir), str(tex_path)],
                check=False,
                capture_output=True,
                timeout=120,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.exception("tectonic compile failed")
            return None
        log = (proc.stdout or b"") + (proc.stderr or b"")
        log_text = (
            log.decode("utf-8", "replace")
            if isinstance(log, (bytes, bytearray)) else str(log)
        )
        if proc.returncode != 0:
            logger.error("tectonic compile failed")
            return None
        built = out_dir / "resume.pdf"
        if not built.is_file():
            return None
        fd, dest = tempfile.mkstemp(suffix=".pdf", prefix="resume_")
        os.close(fd)
        dest_path = Path(dest)
        shutil.copy(built, dest_path)
        return dest_path, log_text


def _pdf_page_count(path: Path) -> int:
    try:
        from pypdf import PdfReader

        return len(PdfReader(str(path)).pages)
    except Exception:
        logger.exception("pypdf page count failed; falling back to byte scan")
        data = path.read_bytes()
        return len(re.findall(rb"/Type\s*/Page\b", data))
