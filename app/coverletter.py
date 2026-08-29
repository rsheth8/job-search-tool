"""Optional one-page cover letter for a posting.

Built only when the user asks (Apply → documents). The layout is a standard
US business letter we own in LaTeX — Claude may draft the paragraphs, never
the template. Facts come from identity + knowledge; we never invent employers
or metrics. Must compile to exactly one page with nothing clipped.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from . import applicant, knowledge, profile as profile_mod, resume_store, resume_tailor
from .config import get_settings

logger = logging.getLogger("coverletter")

_VARIANT = "cover"
_MAX_FIT_ROUNDS = 8

_LETTER_SCHEMA = {
    "type": "object",
    "properties": {
        "greeting": {"type": "string"},
        "paragraphs": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 3,
            "maxItems": 4,
        },
        "closing": {"type": "string"},
    },
    "required": ["greeting", "paragraphs", "closing"],
    "additionalProperties": False,
}

_WEAK_OPEN = re.compile(
    r"^\s*(i am writing to apply|i am excited to apply|to whom it may concern|"
    r"i wish to apply|please accept this letter|i am applying for)\b",
    re.I,
)
_GENERIC_JD = re.compile(
    r"^(?:we(?:'re| are) (?:looking|seeking|hiring|searching)|"
    r"join our (?:team|company)|"
    r"this (?:role|position) (?:is|will)|"
    r"about (?:the|this) (?:role|position)|"
    r"the (?:ideal|right) candidate)\b",
    re.I,
)


@dataclass(frozen=True)
class CoverResult:
    pdf_bytes: bytes
    filename: str
    pages: int
    from_cache: bool = False


@dataclass
class LetterCopy:
    greeting: str
    paragraphs: list[str]
    closing: str


def for_posting(user_id: str, posting: dict) -> CoverResult | None:
    """One-page cover letter PDF for this posting, or None if unavailable."""
    s = get_settings()
    if not s.cover_letter_enabled:
        return None
    posting = _as_dict(posting)
    company = (posting.get("company") or "").strip() or "the company"
    title = (posting.get("title") or "").strip() or "this role"
    description = posting.get("description") or ""
    posting_id = posting.get("id")

    cached = resume_store.find_cached(
        user_id, company, title, _VARIANT,
        posting_id=posting_id, description=description,
    )
    if cached is not None:
        hit = _from_cache(cached, title, company)
        if hit is not None:
            return hit

    ident = applicant.get_identity(user_id)
    copy = _draft_copy(user_id, company, title, description, ident)
    tex = render_tex(ident, company, title, copy)
    fitted = _fit_one_page(ident, company, title, copy, tex)
    if fitted is None:
        return None
    pdf_path, pages, final_tex = fitted
    try:
        pdf_bytes = pdf_path.read_bytes()
    finally:
        pdf_path.unlink(missing_ok=True)
    if pages != 1:
        return None

    resume_store.save(
        user_id, company, title, _VARIANT,
        pdf_bytes=pdf_bytes, tex=final_tex, pages=1,
        posting_id=posting_id, description=description,
    )
    return CoverResult(
        pdf_bytes=pdf_bytes,
        filename=_filename(title, company),
        pages=1,
        from_cache=False,
    )


def _as_dict(posting) -> dict:
    if isinstance(posting, dict):
        return posting
    try:
        return {k: posting[k] for k in posting.keys()}
    except Exception:
        return {}


def _from_cache(row, title: str, company: str) -> CoverResult | None:
    from pathlib import Path

    pdf_path = Path(row["pdf_path"])
    if not pdf_path.is_file():
        return None
    pages = resume_tailor._pdf_page_count(pdf_path)
    if pages != 1:
        return None
    return CoverResult(
        pdf_bytes=pdf_path.read_bytes(),
        filename=_filename(title, company),
        pages=1,
        from_cache=True,
    )


def _filename(title: str, company: str) -> str:
    return (
        f"Cover_Letter_{resume_tailor._slug(title)}_"
        f"{resume_tailor._slug(company)}.pdf"
    )


def _greeting(company: str) -> str:
    if not company or company == "the company":
        return "Dear Hiring Team,"
    return f"Dear {company} Hiring Team,"


def _draft_copy(
    user_id: str, company: str, title: str, description: str, ident: dict,
) -> LetterCopy:
    ident_block = applicant.identity_block(user_id)
    know = knowledge.knowledge_block(
        user_id, context=f"{title} {company} {description}",
    )
    background = profile_mod.profile_text(profile_mod.get_profile(user_id))
    if get_settings().use_llm_router:
        try:
            drafted = _draft_via_claude(
                company, title, description, ident_block, know, background,
            )
            if drafted is not None:
                return drafted
        except Exception:
            logger.exception("cover letter draft failed; using template")
    return _template_copy(company, title, description, ident, know)


def _draft_via_claude(
    company: str, title: str, description: str,
    ident_block: str, know: str, background: str,
) -> LetterCopy | None:
    import anthropic

    from . import llm_budget

    if not llm_budget.consume():
        return None
    s = get_settings()
    client = anthropic.Anthropic(api_key=s.anthropic_api_key)
    desc = (description or "").strip()[:1600]
    resp = client.messages.create(
        model=s.anthropic_model,
        max_tokens=900,
        system=(
            "You write a US job-application cover letter. Best practices:\n"
            "- Exactly 3 short paragraphs (optionally a 4th if needed). "
            "250–400 words total. One page.\n"
            "- Greeting and closing are added by our layout. You only write "
            "the body paragraphs.\n"
            "- Do not start with 'I am writing to apply', 'I am excited to "
            "apply', or 'I am applying for'. Open with a specific reason this "
            "role or company stood out, plus one real proof from the facts.\n"
            "- Middle: 1–2 concrete accomplishments from the candidate's facts, "
            "tied to THIS job description. Never invent employers, projects, or "
            "numbers. If a fact is not in the provided material, omit it.\n"
            "- Close: what you'd contribute, thanks, interest in a conversation. "
            "No 'I am the perfect candidate.' No placeholders.\n"
            "- First person, confident, specific."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Role: {title} at {company}.\n"
                f"Candidate: {ident_block or '(not provided)'}\n"
                f"Background:\n{background or '(not provided)'}\n"
                + (f"What I've done (cite only these):\n{know}\n" if know else "")
                + f"Job description (may be truncated):\n{desc or '(not provided)'}"
            ),
        }],
        output_config={"format": {"type": "json_schema", "schema": _LETTER_SCHEMA}},
    )
    payload = next((b.text for b in resp.content if b.type == "text"), "")
    data = json.loads(payload)
    paras = [str(p).strip() for p in data.get("paragraphs") or [] if str(p).strip()]
    if len(paras) < 3:
        return None
    if _WEAK_OPEN.search(paras[0]):
        paras[0] = _opening(company, title, description)
    return LetterCopy(
        greeting=_greeting(company),
        paragraphs=paras[:4],
        closing="Sincerely,",
    )


def _template_copy(
    company: str, title: str, description: str, ident: dict, know: str,
) -> LetterCopy:
    facts = _fact_lines(know, ident)
    opening = _opening(company, title, description)
    middle = _middle(title, facts, ident)
    close = (
        f"I would welcome a conversation about how I can contribute to the "
        f"{title} team"
        + (f" at {company}" if company != "the company" else "")
        + ". Thank you for your time and consideration."
    )
    return LetterCopy(
        greeting=_greeting(company),
        paragraphs=[opening, middle, close],
        closing="Sincerely,",
    )


def _opening(company: str, title: str, description: str) -> str:
    hook = _jd_hook(description)
    if hook:
        return (
            f"The {title} role at {company} stood out because {hook} "
            f"That is the kind of work I want to do next."
        )
    return (
        f"I want to join {company} as a {title} because the role matches the "
        f"systems and product work I have been building."
    )


def _middle(title: str, facts: list[str], ident: dict) -> str:
    if facts:
        body = " ".join(facts)
        return (
            f"{body} I am looking to bring that same focus to the {title} role."
        )
    role = (ident.get("current_title") or ident.get("discipline") or "").strip()
    school = (ident.get("school") or "").strip()
    bits = []
    if role:
        bits.append(role)
    if school:
        bits.append(f"my work at {school}")
    have = " and ".join(bits) if bits else "the projects I have shipped"
    return (
        f"Recently I have focused on {have}. I would use that experience on "
        f"the problems this {title} role is hiring to solve."
    )


def _fact_lines(know: str, ident: dict) -> list[str]:
    """Up to two stored facts, compressed. Empty if we have nothing real."""
    found: list[str] = []
    if know:
        for line in know.splitlines():
            line = line.strip().lstrip("- ").strip()
            if not line or line.endswith(":"):
                continue
            line = re.sub(r"\s+", " ", line)
            if len(line) > 220:
                line = line[:217].rsplit(" ", 1)[0] + "."
            if not line.endswith("."):
                line += "."
            found.append(line)
            if len(found) == 2:
                break
        if found:
            return found
    title = (ident.get("current_title") or "").strip()
    company = (ident.get("current_company") or "").strip()
    if title and company:
        return [f"I currently work as {title} at {company}."]
    if title:
        return [f"I currently work as {title}."]
    return []


def _jd_hook(description: str) -> str:
    text = re.sub(r"\s+", " ", (description or "").strip())
    if not text:
        return ""
    for sent in re.split(r"(?<=[.!?])\s+", text):
        sent = sent.strip()
        if not sent or _GENERIC_JD.search(sent):
            continue
        sent = sent[:1].lower() + sent[1:]
        if len(sent) > 140:
            sent = sent[:137].rsplit(" ", 1)[0]
        if sent and not sent.endswith((".", "!", "?")):
            sent += "."
        return sent
    return ""


def render_tex(ident: dict, company: str, title: str, copy: LetterCopy) -> str:
    """Canonical business-letter .tex. We never let the model emit this."""
    name = _tex_escape((ident.get("full_name") or "").strip() or "Applicant")
    contact = _tex_escape(_contact_line(ident))
    greeting = _tex_escape(copy.greeting)
    closing = _tex_escape(copy.closing)
    paras = "\n\n".join(_tex_escape(p) for p in copy.paragraphs if p.strip())
    company_tex = _tex_escape(company if company != "the company" else "Hiring Team")
    title_tex = _tex_escape(title)
    contact_block = f"{{\\small {contact}\\par}}\n" if contact else ""

    return rf"""
\documentclass[11pt,letterpaper]{{article}}
\usepackage[margin=1in]{{geometry}}
\pagestyle{{empty}}
\raggedright
\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{0.85em}}
\begin{{document}}

{{\LARGE\bfseries {name}\par}}
{contact_block}\vspace{{8pt}}
\hrule height 0.4pt
\vspace{{14pt}}

\today

\vspace{{12pt}}
Hiring Team\\
{company_tex}

\vspace{{8pt}}
\textit{{Re: {title_tex}}}

\vspace{{10pt}}
{greeting}

{paras}

\vspace{{12pt}}
{closing}

\vspace{{22pt}}
{name}

\vspace{{10pt}}
{{\small Enclosure: Resume}}

\end{{document}}
"""


def _contact_line(ident: dict) -> str:
    loc = (ident.get("location") or "").strip()
    if not loc:
        loc = ", ".join(
            p for p in (ident.get("city"), ident.get("state"), ident.get("country")) if p
        )
    bits = [b for b in (
        loc,
        (ident.get("phone") or "").strip(),
        (ident.get("email") or "").strip(),
        _url_display(ident.get("linkedin") or ""),
        _url_display(ident.get("github") or ""),
    ) if b]
    return " | ".join(bits)


def _url_display(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    return re.sub(r"^https?://(www\.)?", "", url).rstrip("/")


def _tex_escape(text: str) -> str:
    out = (text or "")
    repl = (
        ("\\", r"\textbackslash{}"),
        ("&", r"\&"),
        ("%", r"\%"),
        ("$", r"\$"),
        ("#", r"\#"),
        ("_", r"\_"),
        ("{", r"\{"),
        ("}", r"\}"),
        ("~", r"\textasciitilde{}"),
        ("^", r"\textasciicircum{}"),
    )
    for a, b in repl:
        out = out.replace(a, b)
    return out


def _fit_one_page(
    ident: dict, company: str, title: str, copy: LetterCopy, tex: str,
):
    working = list(copy.paragraphs)
    current_tex = tex
    for _ in range(_MAX_FIT_ROUNDS):
        compiled = resume_tailor._compile_one_page(current_tex)
        if compiled is not None:
            return compiled
        if len(working) > 3:
            working = working[:-1]
        elif working:
            shorter = _shorten(working[-1])
            if shorter == working[-1]:
                return None
            working = working[:-1] + [shorter]
        else:
            return None
        current_tex = render_tex(
            ident, company, title,
            LetterCopy(copy.greeting, working, copy.closing),
        )
    return None


def _shorten(paragraph: str) -> str:
    parts = re.split(r"(?<=[.!?])\s+", paragraph.strip())
    if len(parts) <= 1:
        return paragraph
    return " ".join(parts[:-1])
