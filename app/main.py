"""FastAPI app for the iOS beta.

Session-authenticated chat and apply JSON APIs are the product surface.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response

from .config import get_settings
from .db import init_db
from .errors import install as install_error_handlers


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Background poll loop that delivers due reminders. Started here (not at
    # import) so a bare `import app.main` in tests doesn't spin up a real
    # scheduler. No-op if apscheduler isn't installed.
    _init_sentry()
    # A broken key or model degrades every AI feature to heuristics silently.
    # Log it at boot so it's caught before testers get the build.
    from .llm_health import warn_if_misconfigured

    warn_if_misconfigured()
    from .scheduler import start_scheduler

    start_scheduler()
    yield


def _init_sentry() -> None:
    dsn = get_settings().sentry_dsn.strip()
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError:
        return
    sentry_sdk.init(
        dsn=dsn,
        integrations=[StarletteIntegration(), FastApiIntegration()],
        traces_sample_rate=0.05,
        send_default_pii=False,
    )


app = FastAPI(
    title="JobPilot", version="0.1.0", lifespan=lifespan
)
install_error_handlers(app)

# CORS for local tooling / scripts that hit apply endpoints from a browser.
# The iOS app uses session auth; CORS is not required for native requests.
# Writes are additionally gated by the X-Apply-Token header (see
# _require_apply_token).
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

_s = get_settings()
_cors_raw = _s.apply_cors_origins.strip()
_cors_regex = _s.apply_cors_origin_regex.strip()
_cors_kwargs: dict = {
    "allow_methods": ["GET", "POST", "OPTIONS"],
    "allow_headers": ["*"],
}
if _cors_raw == "*" or (not _cors_raw and not _cors_regex):
    _cors_kwargs["allow_origins"] = ["*"]
else:
    _cors_kwargs["allow_origins"] = [
        o.strip() for o in _cors_raw.split(",") if o.strip()
    ]
    if _cors_regex:
        _cors_kwargs["allow_origin_regex"] = _cors_regex
app.add_middleware(CORSMiddleware, **_cors_kwargs)

# Ensure the schema exists as soon as the app module is imported, regardless of
# how it's launched (uvicorn, TestClient, etc.). init_db() is idempotent.
init_db()


def _require_apply_token(request: Request) -> None:
    """Gate the autofill endpoints when APPLY_API_TOKEN is configured.

    A valid Sign-in-with-Apple session also satisfies the gate. When
    ``AUTH_FAIL_OPEN`` is false, a blank token is no longer a hole.
    """
    from . import auth

    auth.require_apply_access(request)


def _resolve_user(request: Request, user: str | None = None) -> str:
    """Prefer the signed-in session user; never invent a default."""
    from . import auth

    return auth.resolve_user(request, user)


@app.get("/apply/data")
def apply_data(request: Request, user: str | None = None) -> dict:
    """The apply queue plus the top un-staged matches available to stage.

    Each row carries its **fit explanation** — why this job surfaced, in words —
    so the mobile app can show reasons instead of a bare percentage that can't be
    argued with. Computed heuristically from the posting + profile, so it's free.
    """
    from datetime import datetime, timezone

    from . import apply_queue, ats, discovery, fit, jobstore, shortlist
    from . import profile as profile_mod
    from .config import get_settings

    uid = _resolve_user(request, user)
    refresh = (request.query_params.get("refresh") or "").strip().lower()
    if refresh in ("1", "true", "yes"):
        discovery.kick(uid)
    jobstore.wake_snoozed(uid, datetime.now(timezone.utc).isoformat())
    settings = get_settings()
    if settings.job_verify_apply_urls:
        from .jobsources import alive

        alive.close_dead_shortlist(uid, today_n=settings.job_digest_top_n)
    prof = profile_mod.get_profile(uid)
    today_n = settings.job_digest_top_n

    def explain(posting_id: int, score=None) -> dict:
        posting = jobstore.get_posting(uid, posting_id)
        if posting is None:
            return {}
        detail = fit.explain(posting, prof, score=score)
        return {"why": detail["line"], "reasons": detail["reasons"],
                "concerns": detail["concerns"]}

    staged = {it["posting_id"] for it in apply_queue.list_queue(uid)}
    queued = []
    for i, r in enumerate(
        r for r in jobstore.list_review_queue(uid) if r["id"] not in staged
    ):
        queued.append({
            "posting_id": r["id"], "company": r["company"], "title": r["title"],
            "url": r["url"], "score": r["relevance_score"], "source": r["source"],
            "auto_fillable": ats.is_fillable_form(r["url"]),
            "apply_kind": ats.apply_kind(r["url"], r["source"]),
            "apply_today": i < today_n,
            "fresh": shortlist.is_fresh(r["posted_at"]),
            **explain(r["id"], r["relevance_score"]),
        })
    queue = [{**it, **explain(it["posting_id"], it.get("score"))}
             for it in apply_queue.list_queue(uid)]
    return {
        "user": uid, "queued": queued, "queue": queue,
        "discovery": discovery.search_status(uid),
    }


@app.post("/apply/discover")
async def apply_discover(request: Request) -> dict:
    """Start a discovery pass for the signed-in user (quiz / pull-to-refresh).

    Returns immediately. The Apply tab re-reads ``GET /apply/data`` until
    ``discovery.searching`` is false. Rate-limited per user so a chatty
    tester cannot stack ticks.
    """
    from . import discovery

    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — empty body is a valid kick
        body = {}
    uid = _resolve_user(request, body.get("user") if isinstance(body, dict) else None)
    force = bool(body.get("force")) if isinstance(body, dict) else False
    return {"user": uid, **discovery.kick(uid, force=force)}


@app.get("/apply/applications")
def apply_applications(request: Request, user: str | None = None) -> dict:
    """Applications already filed — the tracker half of the Apply tab."""
    from . import store

    uid = _resolve_user(request, user)
    rows = store.list_applications(uid, limit=50)
    return {
        "user": uid,
        "applications": [
            {
                "id": r["id"],
                "company": r["company"],
                "role": r["role"],
                "status": r["status"],
                "applied_at": r["applied_at"],
                "next_follow_up_at": r["next_follow_up_at"],
            }
            for r in rows
        ],
    }


@app.get("/apply/knowledge")
def apply_knowledge(request: Request, user: str | None = None) -> dict:
    """What the assistant knows about you, plus the coverage audit."""
    from . import knowledge

    _require_apply_token(request)
    uid = _resolve_user(request, user)
    return {"user": uid, "items": knowledge.list_all(uid),
            "audit": knowledge.audit(uid)}


@app.post("/apply/knowledge")
async def apply_knowledge_add(request: Request) -> dict:
    """Store one durable fact (experience / project / achievement / strength /
    preference / answer). Returns the saved row, or ok=False for an unknown category."""
    from . import knowledge

    _require_apply_token(request)
    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    item = knowledge.add(uid, body.get("category") or "", body.get("text") or "",
                         label=body.get("label"))
    return {"ok": item is not None, "item": item}


@app.post("/apply/knowledge/remove")
async def apply_knowledge_remove(request: Request) -> dict:
    from . import knowledge

    _require_apply_token(request)
    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    return {"ok": knowledge.remove(uid, int(body["id"]))}


@app.post("/apply/stage")
async def apply_stage(request: Request) -> dict:
    from . import apply_queue

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    return {"ok": apply_queue.stage(uid, int(body["posting_id"]))}


@app.post("/apply/package")
async def apply_package(request: Request) -> dict:
    """Assemble (and cache) the full application package for one staged item."""
    from . import apply_queue

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    from fastapi import HTTPException

    try:
        pid = int(body["posting_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail="That application is missing. Go back and try Preflight again.",
        ) from exc
    pkg = apply_queue.get_package(uid, pid)
    if pkg is None:
        raise HTTPException(
            status_code=404,
            detail="That application isn't ready yet. Go back and tap Preflight again.",
        )
    return pkg


@app.post("/apply/answer/save")
async def apply_answer_save(request: Request) -> dict:
    """Persist a user-edited answer to one of an item's questions (by index)."""
    from . import apply_queue

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    ok = apply_queue.save_answer(
        uid, int(body["posting_id"]), int(body.get("index", 0)), body.get("answer", "")
    )
    return {"ok": ok}


@app.post("/apply/answer/redraft")
async def apply_answer_redraft(request: Request) -> dict:
    """Regenerate a fresh answer for one of an item's questions (by index)."""
    from . import apply_queue

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    answer = apply_queue.redraft_answer(
        uid, int(body["posting_id"]), int(body.get("index", 0))
    )
    return {"answer": answer} if answer is not None else {"error": "not found"}


@app.post("/apply/mark")
async def apply_mark(request: Request) -> dict:
    from . import apply_queue

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    return {"ok": apply_queue.mark(uid, int(body["posting_id"]), body.get("status", ""))}


@app.post("/apply/applied")
async def apply_applied(request: Request) -> dict:
    """Mobile app: the user finished and submitted an application in the in-app
    browser. Log it (application record + posting + queue)."""
    from . import apply_queue, jobstore, store

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    pid = int(body["posting_id"])
    posting = jobstore.get_posting(uid, pid)
    if posting is None:
        return {"ok": False}
    store.create_application(uid, posting["company"] or "Unknown",
                             posting["title"] or "Role", source="mobile")
    jobstore.mark_posting_status(uid, posting["id"], "applied")
    apply_queue.mark(uid, pid, "submitted")
    return {"ok": True}


@app.post("/apply/remove")
async def apply_remove(request: Request) -> dict:
    from . import apply_queue

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    return {"ok": apply_queue.remove(uid, int(body["posting_id"]))}


@app.post("/apply/pass")
async def apply_pass(request: Request) -> dict:
    """Pass on a posting from the phone: unstage it and mark the posting dismissed
    so it leaves both the ready queue and the top-matches list."""
    from . import apply_queue, jobstore

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    pid = int(body["posting_id"])
    # Report honestly, like /apply/snooze and /apply/applied do. This returned
    # ok:True even for a posting that isn't this user's (the write was already
    # correctly skipped), so the client couldn't tell "dismissed" from "not
    # yours" and would cross it off the list anyway.
    if jobstore.get_posting(uid, pid) is None:
        return {"ok": False}
    apply_queue.remove(uid, pid)
    jobstore.mark_posting_status(uid, pid, "dismissed")
    return {"ok": True}


@app.post("/apply/snooze")
async def apply_snooze(request: Request) -> dict:
    """Hide a posting for a while (default 7 days). Unstages it if it was ready;
    discovery and /apply/data wake it when the snooze expires."""
    from datetime import datetime, timedelta, timezone
    from . import apply_queue, jobstore

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    pid = int(body["posting_id"])
    if jobstore.get_posting(uid, pid) is None:
        return {"ok": False}
    try:
        days = int(body.get("days") or 7)
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, 90))
    until = datetime.now(timezone.utc) + timedelta(days=days)
    apply_queue.remove(uid, pid)
    jobstore.snooze_posting(uid, pid, until.isoformat())
    return {"ok": True, "until": until.isoformat()}


@app.post("/apply/promote")
async def apply_promote(request: Request) -> dict:
    """Make this the next ready item (front of the apply queue)."""
    from . import apply_queue

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    return {"ok": apply_queue.promote(uid, int(body["posting_id"]))}


@app.post("/apply/reorder")
async def apply_reorder(request: Request) -> dict:
    """Persist drag-to-reorder on the phone. ``queue`` is ready items; ``matches``
    is the un-staged list. Either key may be omitted."""
    from . import apply_queue, jobstore

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    ok = True
    if "queue" in body and body["queue"] is not None:
        ok = apply_queue.reorder(uid, [int(x) for x in body["queue"]]) and ok
    if "matches" in body and body["matches"] is not None:
        ok = jobstore.reorder_matches(uid, [int(x) for x in body["matches"]]) and ok
    return {"ok": ok}


@app.post("/apply/device")
async def apply_register_device(request: Request) -> dict:
    """Register this phone for push notifications (new matches, approval ready)."""
    from . import push

    _require_apply_token(request)
    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    ok = push.register_device(uid, body.get("token") or "",
                              body.get("platform") or "ios",
                              timezone=body.get("timezone"))
    # `configured` tells the app whether pushes will actually arrive, so it can say
    # so in Settings instead of silently registering into a void.
    return {"ok": ok, "configured": push.configured()}


@app.post("/apply/device/remove")
async def apply_forget_device(request: Request) -> dict:
    from . import push

    _require_apply_token(request)
    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    return {"ok": push.forget_device(uid, body.get("token") or "")}


@app.get("/apply/rules")
def apply_rules(request: Request) -> dict:
    """The field-matching rules (+ formprobe patterns) for the iOS in-app browser."""
    from . import fieldmatch, formprobe

    _require_apply_token(request)
    payload = fieldmatch.rules_payload()
    payload["formprobe"] = formprobe.payload()
    return payload


@app.post("/apply/fill-skips")
async def apply_fill_skips(request: Request) -> dict:
    """Record labels Fill skipped so unmatched ATS wording becomes a phrasing table."""
    from . import filllearn

    _require_apply_token(request)
    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    pid = body.get("posting_id")
    try:
        posting_id = int(pid) if pid is not None and str(pid).strip() != "" else None
    except (TypeError, ValueError):
        posting_id = None
    return {
        "user": uid,
        **filllearn.record_skips(
            uid, body.get("skips") or [],
            url=body.get("url") or "",
            posting_id=posting_id,
        ),
    }


@app.get("/apply/fill-skips")
def apply_fill_skips_list(request: Request, user: str | None = None,
                          limit: int = 50) -> dict:
    from . import filllearn

    _require_apply_token(request)
    uid = _resolve_user(request, user)
    return {"user": uid, "skips": filllearn.list_skips(uid, limit=limit)}


@app.get("/apply/identity")
def apply_identity(request: Request, user: str | None = None) -> dict:
    """The applicant identity map painted onto simple form fields."""
    from . import applicant

    _require_apply_token(request)
    uid = _resolve_user(request, user)
    return {"user": uid, "fields": applicant.autofill_map(uid)}


@app.post("/apply/identity")
async def apply_identity_set(request: Request) -> dict:
    """Save/update applicant identity."""
    from . import applicant

    _require_apply_token(request)
    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    fields = body.get("fields") or {}
    return {"user": uid, "fields": applicant.get_identity(uid),
            "saved": applicant.set_identity(uid, fields)}


@app.get("/apply/setup")
def apply_setup(request: Request, user: str | None = None) -> dict:
    """First-run quiz status (profile + identity coverage)."""
    from . import onboarding

    uid = _resolve_user(request, user)
    return {"user": uid, **onboarding.status(uid)}


@app.post("/apply/setup")
async def apply_setup_set(request: Request) -> dict:
    """Pin a new member in the quiz (start) or let them through (complete)."""
    from fastapi import HTTPException

    from . import onboarding

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    action = (body.get("action") or "").strip().lower()
    if action == "start":
        return {"user": uid, **onboarding.mark_started(uid)}
    if action in ("complete", "done", "finish"):
        from . import discovery

        out = {"user": uid, **onboarding.mark_complete(uid)}
        # First-run: don't wait for the 10-minute scheduler. Force a pass even
        # if they saved roles (and kicked) a few seconds ago.
        discovery.kick(uid, force=True)
        out["discovery"] = discovery.search_status(uid)
        return out
    raise HTTPException(status_code=400, detail="action must be start or complete")


@app.get("/apply/quiz/draft")
def apply_quiz_draft_get(request: Request, user: str | None = None) -> dict:
    """Prefill long quiz answers from stored knowledge (no model call)."""
    from . import onboarding

    _require_apply_token(request)
    uid = _resolve_user(request, user)
    return {"user": uid, "draft": onboarding.quiz_draft(uid)}


@app.post("/apply/quiz/draft")
async def apply_quiz_draft_set(request: Request) -> dict:
    """Same as GET, optionally polished by Claude into first-person answers."""
    from . import onboarding

    _require_apply_token(request)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    uid = _resolve_user(request, body.get("user"))
    polish = bool(body.get("polish"))
    return {"user": uid, "draft": onboarding.quiz_draft(uid, polish=polish)}


@app.get("/apply/profile")
def apply_profile_get(request: Request, user: str | None = None) -> dict:
    from . import profile as profile_mod

    uid = _resolve_user(request, user)
    return {"user": uid, "fields": profile_mod.public_fields(uid),
            "has_profile": profile_mod.has_profile(uid)}


@app.post("/apply/profile")
async def apply_profile_set(request: Request) -> dict:
    """Set search criteria so discovery can tick for this user.

    Partial: a key the caller omits is left alone (``set_profile`` skips ``None``),
    so a save that only carries a résumé summary can't blank out roles or
    locations. An explicit empty string *does* clear that field.
    """
    from . import profile as profile_mod

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    fields = body.get("fields") or body
    profile_mod.set_profile(
        uid,
        roles=fields.get("roles"),
        keywords=fields.get("keywords") or fields.get("roles"),
        locations=fields.get("locations"),
        seniority=fields.get("seniority"),
        resume_summary=fields.get("resume_summary"),
    )
    has = profile_mod.has_profile(uid)
    if has:
        from . import discovery

        discovery.kick(uid)
    return {"user": uid, "fields": profile_mod.public_fields(uid),
            "has_profile": has}


async def _import_payload(request: Request) -> tuple[str, str, str, bytes | None]:
    """JSON ``{text}`` or multipart ``file`` (+ optional ``text``)."""
    ctype = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in ctype:
        form = await request.form()
        uid = _resolve_user(request, form.get("user"))
        upload = form.get("file")
        text = str(form.get("text") or "")
        filename, data = "", None
        if upload is not None and hasattr(upload, "read"):
            filename = getattr(upload, "filename", "") or "upload"
            data = await upload.read()
        return uid, text, filename, data
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    uid = _resolve_user(request, body.get("user"))
    return uid, (body.get("text") or ""), (body.get("filename") or ""), None


@app.post("/apply/import/resume")
async def apply_import_resume(request: Request) -> dict:
    """Scan a resume PDF/text and fill empty profile fields."""
    from fastapi import HTTPException

    from . import profile_import as pi

    _require_apply_token(request)
    uid, text, filename, data = await _import_payload(request)
    try:
        return {"user": uid, **pi.import_resume(
            uid, text=text, filename=filename, data=data)}
    except pi.ProfileImportError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


@app.post("/apply/import/github")
async def apply_import_github(request: Request) -> dict:
    """Fill empty fields from a public GitHub profile + top repos."""
    from fastapi import HTTPException

    from . import profile_import as pi

    _require_apply_token(request)
    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    handle = body.get("username") or body.get("url") or body.get("handle") or ""
    try:
        return {"user": uid, **pi.import_github(uid, handle)}
    except pi.ProfileImportError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


@app.post("/apply/import/linkedin")
async def apply_import_linkedin(request: Request) -> dict:
    """Save a LinkedIn URL and/or scan a LinkedIn PDF / pasted profile text."""
    from fastapi import HTTPException

    from . import profile_import as pi

    _require_apply_token(request)
    ctype = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in ctype:
        form = await request.form()
        uid = _resolve_user(request, form.get("user"))
        url = str(form.get("url") or "")
        text = str(form.get("text") or "")
        upload = form.get("file")
        filename, data = "", None
        if upload is not None and hasattr(upload, "read"):
            filename = getattr(upload, "filename", "") or "upload"
            data = await upload.read()
    else:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        uid = _resolve_user(request, body.get("user"))
        url = body.get("url") or ""
        text = body.get("text") or ""
        filename, data = body.get("filename") or "", None
    try:
        return {"user": uid, **pi.import_linkedin(
            uid, url=url, text=text, filename=filename, data=data)}
    except pi.ProfileImportError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


@app.post("/feedback")
async def feedback_submit(request: Request) -> dict:
    from fastapi import HTTPException

    from . import auth, feedback as fb

    uid = auth.require_user(request)
    body = await request.json()
    text = (body.get("body") or body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="body required")
    row = fb.add(uid, text, context=body.get("context"))
    return {"ok": True, "id": row["id"]}


@app.post("/apply/answer")
async def apply_answer(request: Request) -> dict:
    """Draft an answer to one free-text application question. ``posting_id`` adds
    the JD/company as context; ``company``/``title``/``jd`` can be passed directly
    when the client only has the live page (no posting on file)."""
    from . import applicant, apply_queue, jobstore, outreach
    from . import profile as prof

    _require_apply_token(request)
    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    question = (body.get("question") or "").strip()
    if not question:
        return {"error": "no question"}
    company = body.get("company") or ""
    title = body.get("title") or ""
    description = body.get("jd") or ""
    pid = body.get("posting_id")
    if pid is not None:
        posting = jobstore.get_posting(uid, int(pid))
        if posting is not None:
            company = company or posting["company"] or ""
            title = title or posting["title"] or ""
            description = description or posting["description"] or ""
            apply_queue.stage(uid, int(pid))  # keep it in the queue we're working
    answer = outreach.answer_application_question(
        question, company or "the company", title, description,
        prof.get_profile(uid), identity_block=applicant.identity_block(uid),
    )
    return {"question": question, "answer": answer}


@app.get("/apply/resume")
def apply_resume(request: Request, user: str | None = None, id: int = 0) -> Response:
    """Download the staged item's tailored resume PDF (rebuilt from cache)."""
    from . import apply_queue

    uid = _resolve_user(request, user)
    built = apply_queue.build_resume_bytes(uid, id)
    if built is None:
        return Response(status_code=404, content="no tailored resume")
    pdf, filename = built
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/apply/cover")
def apply_cover(request: Request, user: str | None = None, id: int = 0) -> Response:
    """Download a one-page cover letter PDF for this posting (on demand)."""
    from . import apply_queue

    uid = _resolve_user(request, user)
    built = apply_queue.build_cover_bytes(uid, id)
    if built is None:
        return Response(status_code=404, content="no cover letter")
    pdf, filename = built
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/health")
def health() -> dict:
    from .router import get_router

    from .errors import ping_db

    from . import llm_health

    s = get_settings()
    router = get_router()
    db_ok = ping_db()
    llm_problem = llm_health.config_problem()
    info = {
        "status": "ok" if db_ok else "degraded",
        "router": router.name,
        # Intent parsing is still always heuristic, but Horizon answers the
        # turns the grammar can't -- reporting a bare "heuristic" here hid that.
        "chat_router": "heuristic+horizon" if s.use_llm_router else "heuristic",
        "model": s.anthropic_model if s.use_llm_router else None,
        "db": s.database_path,
        "db_ok": db_ok,
        # "wal" or the deployment is serialising every reader behind the
        # discovery loop. Unverifiable without SSH otherwise, and it is a
        # property of the *file* -- a restored snapshot can land without it.
        "db_journal": _journal_mode_safe(),
    }
    # Whether paid calls are actually *working*, not just configured. Counters
    # are per-process, so they reset on deploy and each machine reports its own.
    info["llm"] = {
        "configured": s.use_llm_router,
        "model_valid": llm_health.model_looks_valid(s.anthropic_model),
        "problem": llm_problem,
        **llm_health.snapshot(),
        "caps": {
            "per_user_per_day": s.llm_max_calls_per_user_per_day,
            "chat": s.llm_cap_chat,
            "discovery": s.llm_cap_discovery,
            "draft": s.llm_cap_draft,
            "parse": s.llm_cap_parse,
            "quiz": s.llm_cap_quiz,
        },
    }
    if llm_problem and s.anthropic_api_key.strip():
        # Misconfigured-but-keyed is a mistake worth flagging in the top line.
        info["status"] = "degraded"
    # Surface token usage so cost/efficiency is observable at a glance.
    if hasattr(router, "usage"):
        info["usage"] = router.usage
    from .scheduler import is_running

    info["reminder_scheduler"] = "running" if is_running() else "stopped"
    info["reminder_delivery"] = "app"

    from . import catalog, discovery, jobstore

    from .jobsources import directory as dir_src

    info["discovery"] = {
        "sources_enabled": s.job_sources,
        "alert_mode": s.job_alert_mode_normalized,
        "wide_rss": s.job_wide_rss_enabled,
        "wide_directory": s.job_wide_directory_enabled,
        "wide_swelist": s.job_wide_swelist_enabled,
        "wide_yc": s.job_wide_yc_enabled,
        "ghost_filter": s.ghost_filter_enabled,
        "eligibility_filter": s.eligibility_filter_enabled,
        "reranker": s.reranker_enabled,
        "verify_apply_urls": s.job_verify_apply_urls,
        "catalog_probe": s.job_catalog_probe_enabled,
        "directory_boards": dir_src.board_count(),
        "catalog": catalog.stats(),
        "tracked_boards": jobstore.tracked_count(),
        "poll_seconds": s.job_poll_seconds,
        "relevance_threshold": s.job_relevance_threshold,
        "last_tick": discovery.last_tick_at,
        "postings": jobstore.global_counts_by_status(),
    }
    info["auth"] = {
        "fail_open": s.auth_fail_open,
        "allowlist": bool(s.allowed_emails),
        "dev_login": s.auth_allow_dev_login,
        "sentry": bool(s.sentry_dsn.strip()),
        "llm_user_cap": s.llm_max_calls_per_user_per_day,
        "email_signup": s.auth_allow_email_signup,
        "methods": (
            ["apple"] + (["email"] if s.auth_allow_email_signup else [])
            + (["dev"] if s.auth_allow_dev_login else [])
        ),
    }
    info["beta"] = {
        "invite_ready": (
            not s.auth_fail_open
            and not s.auth_allow_dev_login
            and bool(s.allowed_emails)
            and info["reminder_delivery"] == "app"
        ),
        # Separate from invite_ready on purpose: a misconfigured model is a
        # quality problem, not a security one. Both should be true before
        # handing out builds. GET /health/llm proves the key actually works.
        "llm_ready": llm_problem is None,
    }
    # Two of the remaining beta blockers — base résumés on the volume and APNs
    # — were not observable from outside the machine at all, so "did the upload
    # land?" was answered by SSH and guesswork. Both fail soft at runtime (a
    # missing .tex skips tailoring, an incomplete APNS_* set makes push a
    # no-op), which is exactly why they need to be visible here.
    from .resume_tailor import _VARIANTS, resume_dir

    tex_dir = resume_dir()
    try:
        present = sorted(
            f"{v}.tex" for v in _VARIANTS if (tex_dir / f"{v}.tex").is_file()
        )
    except OSError:
        present = []
    info["resume"] = {
        "enabled": s.resume_tailor_enabled,
        "dir": str(tex_dir),
        "bases": present,
        "expected": sorted(f"{v}.tex" for v in _VARIANTS),
    }

    apns_missing = sorted(
        name for name, value in (
            ("APNS_KEY_ID", s.apns_key_id),
            ("APNS_TEAM_ID", s.apns_team_id),
            ("APNS_BUNDLE_ID", s.apns_bundle_id),
        ) if not value.strip()
    )
    # The key can arrive either way, so neither name alone is "missing".
    if not s.apns_key_source:
        apns_missing.append("APNS_KEY_PEM (or APNS_KEY_PATH)")
    info["push"] = {
        "enabled": s.push_enabled,
        "active": s.push_active,
        "sandbox": s.apns_use_sandbox,
        "key_source": s.apns_key_source,
        "missing": sorted(apns_missing),
    }

    info["dependencies"] = _dependency_report()
    if info["dependencies"]["missing"]:
        info["status"] = "degraded"
    return info


def _journal_mode_safe() -> str:
    from .db import journal_mode

    try:
        return journal_mode()
    except Exception:  # noqa: BLE001 - /health must answer even when the DB won't
        return "unknown"


def _dependency_report() -> dict:
    """Optional deps that enabled features hard-depend on.

    Resume tailoring and cover letters are on by default and need both pypdf
    and tectonic. Without them the app logs and skips -- correct, but silent, so
    "why did resumes stop working" was only answerable from log archaeology.
    Push needs pyjwt + cryptography to sign the APNs token, and h2 to send it.
    """
    import importlib.util

    from .resume_tailor import resolve_tectonic

    s = get_settings()

    def have(mod: str) -> bool:
        try:
            return importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            return False

    # resolve_tectonic honours TECTONIC_BIN, $PATH, then the repo .cache -- a
    # bare which("tectonic") would report the deploy image's binary as missing.
    pypdf, tectonic = have("pypdf"), resolve_tectonic() is not None
    jwt_ok, crypto_ok = have("jwt"), have("cryptography")
    # APNs is HTTP/2 only. Without h2, httpx raises inside the per-device try in
    # push.send, which swallows it -- so every notification silently fails to
    # deliver while the config looks perfect. Name it here.
    h2_ok = have("h2")
    report = {
        "pypdf": pypdf,
        "tectonic": tectonic,
        "pyjwt": jwt_ok,
        "cryptography": crypto_ok,
        "h2": h2_ok,
    }
    missing = []
    if (s.resume_tailor_enabled or s.cover_letter_enabled):
        if not pypdf:
            missing.append("pypdf (resume/cover PDFs will not be served)")
        if not tectonic:
            missing.append("tectonic (resume/cover PDFs cannot be compiled)")
    if s.push_enabled and not (jwt_ok and crypto_ok):
        missing.append("pyjwt+cryptography (push notifications cannot be signed)")
    if s.push_enabled and not h2_ok:
        missing.append("h2 (APNs is HTTP/2 only; deliveries will silently fail)")
    report["missing"] = missing
    return report


@app.get("/health/llm")
def health_llm(request: Request) -> dict:
    """Prove the Anthropic key and model actually work: one tiny real call.

    Gated because it spends a request. Shape validation can't distinguish a
    revoked key from a good one, so this is the check the launch checklist's
    "valid ANTHROPIC_API_KEY" really needs.
    """
    from . import auth, llm_health

    auth.require_apply_access(request)
    return {"probe": llm_health.probe(), **llm_health.snapshot()}


# ---------------------------------------------------------------------------
# Auth (Sign in with Apple) + in-app chat
# ---------------------------------------------------------------------------

@app.post("/auth/apple")
async def auth_apple(request: Request) -> dict:
    """Exchange an Apple identity token for an app session."""
    from . import auth

    body = await request.json()
    identity = (body.get("identity_token") or body.get("id_token") or "").strip()
    if not identity:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="identity_token required")
    return auth.sign_in_with_apple(
        identity,
        email=(body.get("email") or None),
        display_name=(body.get("display_name") or body.get("name") or None),
    )


@app.post("/auth/signup")
async def auth_signup(request: Request) -> dict:
    """Create an email + password account and return a session."""
    from . import auth

    body = await request.json()
    return auth.sign_up_email(
        (body.get("email") or "").strip(),
        body.get("password") or "",
        display_name=(body.get("display_name") or body.get("name") or None),
    )


@app.post("/auth/login")
async def auth_login(request: Request) -> dict:
    """Exchange email + password for a session."""
    from . import auth

    body = await request.json()
    return auth.sign_in_email(
        (body.get("email") or "").strip(),
        body.get("password") or "",
    )


@app.post("/auth/password")
async def auth_change_password(request: Request) -> dict:
    """Rotate the password on an email account (session required)."""
    from . import auth

    uid = auth.require_user(request)
    body = await request.json()
    auth.change_password(
        uid,
        body.get("current_password") or "",
        body.get("new_password") or "",
    )
    return {"ok": True}


@app.post("/auth/dev")
async def auth_dev(request: Request) -> dict:
    """Dev-only session mint (AUTH_ALLOW_DEV_LOGIN=true)."""
    from . import auth

    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    return auth.sign_in_dev(
        display_name=body.get("display_name"),
        user_id=body.get("user_id"),
    )


@app.get("/auth/me")
def auth_me(request: Request) -> dict:
    from . import auth

    uid = auth.require_user(request)
    user = auth.get_user(uid)
    return {"user": {
        "id": user["id"],
        "email": user.get("email"),
        "display_name": user.get("display_name"),
    } if user else {"id": uid}}


@app.post("/auth/logout")
def auth_logout(request: Request) -> dict:
    from . import auth

    token = auth.bearer_token(request)
    if token:
        auth.revoke_session(token)
    return {"ok": True}


@app.get("/chat/history")
def chat_history(
    request: Request, limit: int = 100, before_id: int | None = None,
) -> dict:
    from . import auth, chat

    uid = auth.require_user(request)
    return {"user": uid, "messages": chat.history(uid, limit=limit, before_id=before_id)}


@app.post("/chat")
async def chat_send(request: Request) -> dict:
    """Send a message to the assistant (session required). Heuristic NLU."""
    from . import auth, chat

    uid = auth.require_user(request)
    body = await request.json()
    text = (body.get("text") or body.get("body") or "").strip()
    if not text:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="text required")
    result = chat.send(uid, text)
    return {"user": uid, **result}


@app.post("/agent")
async def agent_send(request: Request) -> dict:
    """Execute a structured on-device action (session required).

    Body: ``{action, slots, raw_text}``. UNKNOWN / low confidence falls back
    to heuristic parse of ``raw_text``. Same transcript as POST /chat.
    """
    from fastapi import HTTPException

    from . import auth, chat

    uid = auth.require_user(request)
    body = await request.json()
    raw = (body.get("raw_text") or body.get("text") or body.get("body") or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="raw_text required")
    action = (body.get("action") or body.get("intent") or "UNKNOWN")
    slots = body.get("slots") if isinstance(body.get("slots"), dict) else {}
    result = chat.send_action(uid, str(action), slots, raw)
    return {"user": uid, **result}


@app.post("/chat/clear")
def chat_clear(request: Request) -> dict:
    """Wipe this user's chat transcript and any in-flight command."""
    from . import auth, chat

    uid = auth.require_user(request)
    deleted = chat.clear(uid)
    return {"user": uid, "deleted": deleted, "ok": True}
