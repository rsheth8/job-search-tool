"""Semi-auto application queue (Track C).

Stage postings you want to apply to, assemble a review-ready package for each
(apply link + drafted "why I'm a fit" answers + tailored resume), and track each
item through ``staged -> ready -> submitted``.

We **never submit a form on the user's behalf** — submission stays a human
action. This layer removes the busywork *before* the click: it pre-builds the
application materials so the user just reviews, tweaks, and sends. (The optional
browser form-fill that drives an ATS page is a separate, opt-in step that always
pauses for a final confirmation.)

Package assembly reuses the existing apply-flow builders and inherits their
fail-open behaviour: ``outreach.draft_application_answers`` falls back to a clean
template with no API key, and ``resume_tailor.tailor_for_posting`` returns nothing
when tailoring is disabled. Built answers/resume are cached on the row so
re-opening an item never re-bills the LLM.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from . import ats, jobstore, outreach, profile as profile_mod
from .db import connect

logger = logging.getLogger("apply_queue")

STATUSES = ("staged", "ready", "submitted")

# The free-text questions most applications ask, pre-answered so they're ready to
# review/paste. Tailored per posting (company/title interpolated; answers grounded
# in the JD). The browser extension still answers a form's *actual* questions live;
# this gives the phone preview strong answers without the form in front of you.
COMMON_QUESTIONS = (
    "Why do you want to work at {company}?",
    "Why are you a strong fit for the {title} role?",
    "Tell us about a relevant project or accomplishment.",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stage(user_id: str, posting_id: int) -> bool:
    """Add a posting to the apply queue. Idempotent — returns False if it was
    already staged or the posting doesn't belong to the user."""
    if jobstore.get_posting(user_id, posting_id) is None:
        return False
    now = _now()
    with connect() as conn:
        sort_order = _front_sort_order(conn, user_id)
        cur = conn.execute(
            "INSERT OR IGNORE INTO apply_queue "
            "(user_id, posting_id, status, created_at, updated_at, sort_order) "
            "VALUES (?, ?, 'staged', ?, ?, ?)",
            (user_id, posting_id, now, now, sort_order),
        )
        return cur.rowcount > 0


def remove(user_id: str, posting_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM apply_queue WHERE user_id = ? AND posting_id = ?",
            (user_id, posting_id),
        )
        return cur.rowcount > 0


def _front_sort_order(conn, user_id: str) -> int:
    row = conn.execute(
        "SELECT MIN(sort_order) FROM apply_queue WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    min_so = row[0] if row is not None else None
    return (min_so - 1) if min_so is not None else 0


def promote(user_id: str, posting_id: int) -> bool:
    """Move a staged item to Next. If it isn't staged yet, stage it."""
    if jobstore.get_posting(user_id, posting_id) is None:
        return False
    now = _now()
    with connect() as conn:
        front = _front_sort_order(conn, user_id)
        cur = conn.execute(
            "UPDATE apply_queue SET sort_order = ?, updated_at = ? "
            "WHERE user_id = ? AND posting_id = ?",
            (front, now, user_id, posting_id),
        )
        if cur.rowcount > 0:
            return True
    return stage(user_id, posting_id)


def reorder(user_id: str, posting_ids: list[int]) -> bool:
    """Persist a user-defined ready order. Unknown or foreign ids are skipped."""
    if not posting_ids:
        return True
    now = _now()
    with connect() as conn:
        owned = {
            r["posting_id"]
            for r in conn.execute(
                "SELECT posting_id FROM apply_queue WHERE user_id = ?",
                (user_id,),
            )
        }
        ordered = [int(pid) for pid in posting_ids if int(pid) in owned]
        if not ordered:
            return False
        for i, pid in enumerate(ordered):
            conn.execute(
                "UPDATE apply_queue SET sort_order = ?, updated_at = ? "
                "WHERE user_id = ? AND posting_id = ?",
                (i, now, user_id, pid),
            )
        return True


def mark(user_id: str, posting_id: int, status: str) -> bool:
    """Advance an item to 'ready' or 'submitted'. Returns False for an unknown
    status or a missing item. 'submitted' only ever reflects the user confirming
    they sent the application — it never triggers a submission."""
    if status not in STATUSES:
        return False
    with connect() as conn:
        cur = conn.execute(
            "UPDATE apply_queue SET status = ?, updated_at = ? "
            "WHERE user_id = ? AND posting_id = ?",
            (status, _now(), user_id, posting_id),
        )
        return cur.rowcount > 0


def list_queue(user_id: str, *, status: str | None = None) -> list[dict]:
    """Queue items joined with their posting (company/title/url/score/source),
    user order first (then newest). Optional ``status`` filter. Items whose
    posting was deleted are skipped."""
    sql = (
        "SELECT q.posting_id, q.status, q.questions_json, q.resume_path, q.updated_at, "
        "       p.company, p.title, p.url, p.source, p.relevance_score "
        "FROM apply_queue q JOIN job_postings p ON p.id = q.posting_id "
        "WHERE q.user_id = ? "
    )
    params: list = [user_id]
    if status is not None:
        sql += "AND q.status = ? "
        params.append(status)
    sql += "ORDER BY COALESCE(q.sort_order, 0) ASC, q.created_at DESC"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            "posting_id": r["posting_id"],
            "status": r["status"],
            "company": r["company"],
            "title": r["title"],
            "url": r["url"],
            "source": r["source"],
            "score": r["relevance_score"],
            "auto_fillable": ats.is_fillable_form(r["url"]),
            "apply_kind": ats.apply_kind(r["url"], r["source"]),
            "has_answers": bool(r["questions_json"]),
            "has_resume": bool(r["resume_path"]) and r["resume_path"] != _RESUME_NONE,
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def get_package(user_id: str, posting_id: int, *, prof=None) -> dict | None:
    """Assemble (and cache) the full application package: apply link, a tailored
    answer for each common question, the applicant identity, and a tailored resume.
    Best-effort — answers fall back to templates, resume to None. Returns None if
    the item or its posting is gone."""
    with connect() as conn:
        row = conn.execute(
            "SELECT status, resume_path FROM apply_queue "
            "WHERE user_id = ? AND posting_id = ?",
            (user_id, posting_id),
        ).fetchone()
    if row is None:
        return None
    posting = jobstore.get_posting(user_id, posting_id)
    if posting is None:
        return None
    if prof is None:
        prof = profile_mod.get_profile(user_id)

    company = posting["company"] or "the company"
    title = posting["title"] or "Role"
    questions = get_questions(user_id, posting_id, prof=prof)
    resume = _ensure_resume(user_id, posting_id, company, title, posting,
                            row["resume_path"])

    from . import applicant

    return {
        "posting_id": posting_id,
        "status": row["status"],
        "company": company,
        "title": title,
        "url": posting["url"] or "",
        "source": posting["source"],
        "score": posting["relevance_score"],
        "questions": questions,  # [{question, answer}] — one tailored answer each
        "resume": resume,        # {filename, variant, pages} or None
        # The facts that will fill the form's simple fields — shown so the user can
        # confirm them at a glance before applying.
        "identity": applicant.autofill_map(user_id),
    }


def get_questions(user_id: str, posting_id: int, *, prof=None) -> list[dict]:
    """The application's common questions with a tailored answer each — drafted
    (one batched LLM call) on first request, then cached on the row. Returns
    ``[{question, answer}]``; [] if the item/posting is gone."""
    with connect() as conn:
        row = conn.execute(
            "SELECT questions_json FROM apply_queue WHERE user_id = ? AND posting_id = ?",
            (user_id, posting_id),
        ).fetchone()
    if row is None:
        return []
    cached = _decode_json(row["questions_json"])
    if cached:
        return cached
    posting = jobstore.get_posting(user_id, posting_id)
    if posting is None:
        return []
    if prof is None:
        prof = profile_mod.get_profile(user_id)
    from . import applicant

    from . import knowledge

    company = posting["company"] or "the company"
    title = posting["title"] or "Role"
    prompts = [q.format(company=company, title=title) for q in COMMON_QUESTIONS]

    # Deterministic first: a question you've already answered well is reused
    # verbatim — no model call, no cost, no variance. Only the rest get drafted.
    answers: list[str | None] = [knowledge.canned_answer(user_id, q) for q in prompts]
    todo = [i for i, a in enumerate(answers) if a is None]
    if todo:
        drafted = outreach.draft_question_answers(
            [prompts[i] for i in todo], company, title, posting["description"], prof,
            identity_block=applicant.identity_block(user_id),
            knowledge_block=knowledge.knowledge_block(
                user_id, context=_posting_context(posting)),
        )
        for i, a in zip(todo, drafted):
            answers[i] = a

    qs = [{"question": q, "answer": a or ""} for q, a in zip(prompts, answers)]
    _save_questions(user_id, posting_id, qs)
    return qs


def save_answer(user_id: str, posting_id: int, index: int, answer: str) -> bool:
    """Persist a user-edited answer to question ``index``. False if the item or
    index is out of range."""
    qs = get_questions(user_id, posting_id)
    if not (0 <= index < len(qs)):
        return False
    qs[index]["answer"] = answer
    return _save_questions(user_id, posting_id, qs)


def redraft_answer(user_id: str, posting_id: int, index: int, *, prof=None) -> str | None:
    """Regenerate a fresh answer for question ``index`` only. None if the item or
    index is gone."""
    qs = get_questions(user_id, posting_id, prof=prof)
    if not (0 <= index < len(qs)):
        return None
    posting = jobstore.get_posting(user_id, posting_id)
    if posting is None:
        return None
    if prof is None:
        prof = profile_mod.get_profile(user_id)
    from . import applicant, knowledge

    answer = outreach.answer_application_question(
        qs[index]["question"], posting["company"] or "the company",
        posting["title"] or "Role", posting["description"], prof,
        identity_block=applicant.identity_block(user_id),
        knowledge_block=knowledge.knowledge_block(
            user_id, context=_posting_context(posting)),
    )
    qs[index]["answer"] = answer
    _save_questions(user_id, posting_id, qs)
    return answer


def _save_questions(user_id: str, posting_id: int, qs: list[dict]) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE apply_queue SET questions_json = ?, updated_at = ? "
            "WHERE user_id = ? AND posting_id = ?",
            (json.dumps(qs), _now(), user_id, posting_id),
        )
        return cur.rowcount > 0


def _posting_context(posting) -> str:
    """Title + company + JD, so experience and projects can be ranked per role."""
    return " ".join(
        part for part in (
            posting["title"] or "",
            posting["company"] or "",
            posting["description"] or "",
        ) if part
    )


def _decode_json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def build_resume_bytes(user_id: str, posting_id: int) -> tuple[bytes, str] | None:
    """(pdf_bytes, filename) for the item's tailored resume, or None. Backed by the
    resume_store cache, so serving a download doesn't rebuild the PDF."""
    posting = jobstore.get_posting(user_id, posting_id)
    if posting is None:
        return None
    result = _build_resume(user_id, posting["company"] or "the company",
                           posting["title"] or "Role", posting)
    return (result.pdf_bytes, result.filename) if result else None


def build_cover_bytes(user_id: str, posting_id: int) -> tuple[bytes, str] | None:
    """(pdf_bytes, filename) for an optional one-page cover letter, or None.

    Built only when the user asks — not part of ``get_package``. Cached the
    same way as the résumé (posting + JD hash, variant ``cover``).
    """
    posting = jobstore.get_posting(user_id, posting_id)
    if posting is None:
        return None
    try:
        from . import coverletter

        result = coverletter.for_posting(user_id, posting)
    except Exception:  # noqa: BLE001 — packaging never hard-fails on the letter
        logger.warning("cover letter failed for posting %s", posting_id,
                       exc_info=True)
        return None
    return (result.pdf_bytes, result.filename) if result else None


def _build_resume(user_id: str, company: str, title: str, posting):
    """Best-effort one-page tailored resume (a ``TailorResult``), or None when
    tailoring is disabled/unavailable. Cached by resume_store, so repeat calls for
    the same posting are cheap."""
    try:
        from . import resume_tailor

        return resume_tailor.tailor_for_posting(
            user_id, company, title, posting["description"], posting_id=posting["id"]
        )
    except Exception:  # noqa: BLE001 — packaging never hard-fails on the resume
        logger.warning("resume tailoring failed for posting %s", posting["id"],
                       exc_info=True)
        return None


_RESUME_NONE = "__none__"  # sentinel: tailoring was attempted and yielded nothing


def _ensure_resume(user_id: str, posting_id: int, company: str, title: str,
                   posting, cached: str | None) -> dict | None:
    """Resume metadata for the item — build + cache it on first request, then reuse.
    A sentinel records 'no resume' so we don't retry a disabled build every load."""
    if cached == _RESUME_NONE:
        return None
    meta = _decode_json(cached)
    if meta is not None:
        return meta
    result = _build_resume(user_id, company, title, posting)
    meta = ({"filename": result.filename, "variant": result.variant,
             "pages": result.pages} if result else None)
    with connect() as conn:
        conn.execute(
            "UPDATE apply_queue SET resume_path = ?, updated_at = ? "
            "WHERE user_id = ? AND posting_id = ?",
            (json.dumps(meta) if meta else _RESUME_NONE, _now(), user_id, posting_id),
        )
    return meta

