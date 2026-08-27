"""FastAPI app exposing the Twilio SMS webhook.

Twilio POSTs application/x-www-form-urlencoded with `From` and `Body`.
We reply with TwiML so the user gets an immediate SMS back — no outbound
Twilio credentials required for Phase 1.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from xml.sax.saxutils import escape

from fastapi import BackgroundTasks, FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse

from .config import get_settings
from .db import init_db
from .engine import handle_sms


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Background poll loop that delivers due reminders. Started here (not at
    # import) so a bare `import app.main` in tests doesn't spin up a real
    # scheduler. No-op if apscheduler isn't installed.
    _init_sentry()
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
    title="Job Search SMS Intelligence", version="0.1.0", lifespan=lifespan
)

# CORS for the application-autofill extension: its content script runs on ATS
# origins (greenhouse.io, lever.co, …) and calls /apply/* cross-origin. Writes are
# additionally gated by the X-Apply-Token header (see _require_apply_token).
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
    """Prefer the signed-in session user; never fall back to default_user()."""
    from . import auth

    return auth.resolve_user(request, user)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: str | None = None) -> HTMLResponse:
    """Read-only pipeline dashboard. Session user in prod; ``?user=`` locally."""
    from . import auth
    from . import dashboard as dash

    return HTMLResponse(dash.render(auth.html_user(request, user)))


@app.get("/train", response_class=HTMLResponse)
def train_page(request: Request, user: str | None = None) -> HTMLResponse:
    """Tinder-style swipe trainer to bootstrap the re-ranker on real postings."""
    from . import auth
    from . import trainer

    return HTMLResponse(trainer.render_page(auth.html_user(request, user)))


@app.get("/train/deck")
def train_deck(
    request: Request,
    user: str | None = None, n: int = 15, diverse: bool = False,
    mode: str | None = None,
) -> dict:
    """Return scored cards FAST (no summaries) so the deck shows instantly; the
    client fetches summaries lazily via /train/summaries.

    ``mode`` picks the deck strategy: 'best' (top matches), 'mix' (spread for
    balanced training), or 'learn' (active learning — most-uncertain first).
    ``diverse`` is kept for back-compat (== mode 'mix')."""
    from . import trainer

    uid = _resolve_user(request, user)
    cards = trainer.build_deck(
        uid, limit=n,
        diverse=diverse or mode == "mix",
        uncertain=mode == "learn",
    )
    return {"user": uid, "cards": cards, "stats": trainer.stats(uid)}


@app.post("/train/summaries")
async def train_summaries(request: Request) -> dict:
    """Generate (batched + cached) the plain-language summaries for the given
    cards. Returns a map keyed by 'source:external_id' so the client fills them in
    after the card is already on screen."""
    from . import insights, profile as prof

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    items = body.get("items") or []
    enriched = insights.enrich(items, prof.profile_text(prof.get_profile(uid)))
    fields = ("about", "tldr", "level", "skills", "fit")
    return {"summaries": {
        f"{c.get('source')}:{c.get('external_id')}": {k: c.get(k) for k in fields if c.get(k)}
        for c in enriched
    }}


@app.post("/train/label")
async def train_label(request: Request) -> dict:
    """Record one swipe, retrain the model if there's enough signal, return stats."""
    from . import profile as prof
    from . import reranker, trainer

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    item = body.get("item") or {}
    label = body.get("label", "pass")
    trainer.record_label(uid, item, label)
    reranker.maybe_retrain(uid, prof.get_profile(uid))
    return trainer.stats(uid)


@app.get("/apply")
def apply_page(request: Request, user: str | None = None) -> HTMLResponse:
    """Semi-auto application queue: staged matches with pre-assembled packages."""
    from . import apply_queue, auth

    return HTMLResponse(apply_queue.render_page(auth.html_user(request, user)))


@app.get("/apply/data")
def apply_data(request: Request, user: str | None = None) -> dict:
    """The apply queue plus the top un-staged matches available to stage.

    Each row carries its **fit explanation** — why this job surfaced, in words —
    so the mobile app can show reasons instead of a bare percentage that can't be
    argued with. Computed heuristically from the posting + profile, so it's free.
    """
    from . import apply_queue, fit, jobstore
    from . import profile as profile_mod

    uid = _resolve_user(request, user)
    prof = profile_mod.get_profile(uid)

    def explain(posting_id: int, score=None) -> dict:
        posting = jobstore.get_posting(uid, posting_id)
        if posting is None:
            return {}
        detail = fit.explain(posting, prof, score=score)
        return {"why": detail["line"], "reasons": detail["reasons"],
                "concerns": detail["concerns"]}

    staged = {it["posting_id"] for it in apply_queue.list_queue(uid)}
    from . import ats
    queued = [
        {"posting_id": r["id"], "company": r["company"], "title": r["title"],
         "url": r["url"], "score": r["relevance_score"], "source": r["source"],
         "auto_fillable": ats.is_fillable_form(r["url"]),
         **explain(r["id"], r["relevance_score"])}
        for r in jobstore.list_review_queue(uid) if r["id"] not in staged
    ]
    queue = [{**it, **explain(it["posting_id"], it.get("score"))}
             for it in apply_queue.list_queue(uid)]
    return {"user": uid, "queued": queued, "queue": queue}


@app.get("/apply/inflight")
def apply_inflight(request: Request, user: str | None = None) -> dict:
    """Everything the submit worker is currently handling, for the mobile in-flight
    view. Same rows the dashboard and the Slack reply are built from."""
    from . import dashboard as dash
    from . import fill_requests

    _require_apply_token(request)
    uid = _resolve_user(request, user)
    rows = dash.in_flight_rows(uid)
    # Attach the request id + preview so the phone can approve without a second call.
    by_posting = {r["posting_id"]: r for r in fill_requests.list_active(uid)}
    for row in rows:
        req = by_posting.get(row["id"])
        if req is not None:
            row["request_id"] = req["id"]
            row["status"] = req["status"]
            row["preview"] = req.get("preview")
    return {"user": uid, "inflight": rows}


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
    """Store one durable fact (project / achievement / strength / preference /
    answer). Returns the saved row, or ok=False for an unknown category."""
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
    pkg = apply_queue.get_package(uid, int(body["posting_id"]))
    return pkg or {"error": "not found"}


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
    browser. Log it (application record + posting + queue), like the worker does."""
    from . import apply_queue, jobstore, store

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    pid = int(body["posting_id"])
    posting = jobstore.get_posting(uid, pid)
    if posting is None:
        return {"ok": False}
    store.create_application(uid, posting["company"] or "Unknown",
                             posting["title"] or "Role", source="mobile")
    jobstore.mark_posting_status(posting["id"], "applied")
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
    apply_queue.remove(uid, pid)
    if jobstore.get_posting(uid, pid) is not None:
        jobstore.mark_posting_status(pid, "dismissed")
    return {"ok": True}


@app.post("/apply/device")
async def apply_register_device(request: Request) -> dict:
    """Register this phone for push notifications (new matches, approval ready)."""
    from . import push

    _require_apply_token(request)
    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    ok = push.register_device(uid, body.get("token") or "",
                              body.get("platform") or "ios")
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
    """The field-matching rules (+ formprobe patterns), so the extension and the
    iOS browser stop carrying hand-ported copies that drift out of date."""
    from . import fieldmatch, formprobe

    _require_apply_token(request)
    payload = fieldmatch.rules_payload()
    payload["formprobe"] = formprobe.payload()
    return payload


@app.get("/apply/identity")
def apply_identity(request: Request, user: str | None = None) -> dict:
    """The applicant identity map the extension paints onto simple form fields."""
    from . import applicant

    _require_apply_token(request)
    uid = _resolve_user(request, user)
    return {"user": uid, "fields": applicant.autofill_map(uid)}


@app.post("/apply/identity")
async def apply_identity_set(request: Request) -> dict:
    """Save/update applicant identity (used by the extension options page)."""
    from . import applicant

    _require_apply_token(request)
    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    fields = body.get("fields") or {}
    return {"user": uid, "fields": applicant.get_identity(uid),
            "saved": applicant.set_identity(uid, fields)}


@app.get("/apply/setup")
def apply_setup(request: Request, user: str | None = None) -> dict:
    """First-run wizard status (profile + identity coverage)."""
    from . import onboarding

    uid = _resolve_user(request, user)
    return {"user": uid, **onboarding.status(uid)}


@app.get("/apply/profile")
def apply_profile_get(request: Request, user: str | None = None) -> dict:
    from . import profile as profile_mod

    uid = _resolve_user(request, user)
    return {"user": uid, "fields": profile_mod.public_fields(uid),
            "has_profile": profile_mod.has_profile(uid)}


@app.post("/apply/profile")
async def apply_profile_set(request: Request) -> dict:
    """Set search criteria so discovery can tick for this user."""
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
    )
    return {"user": uid, "fields": profile_mod.public_fields(uid),
            "has_profile": profile_mod.has_profile(uid)}


@app.post("/feedback")
async def feedback_submit(request: Request) -> dict:
    from fastapi import HTTPException

    from . import auth, feedback as fb

    uid = auth.require_user(request)
    body = await request.json()
    text = (body.get("body") or body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="body required")
    row = fb.add(uid, text)
    return {"ok": True, "id": row["id"]}


@app.post("/apply/answer")
async def apply_answer(request: Request) -> dict:
    """Draft an answer to one free-text application question. ``posting_id`` adds
    the JD/company as context; ``company``/``title``/``jd`` can be passed directly
    when the extension only has the live page (no posting on file)."""
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


@app.post("/apply/autosubmit")
async def apply_autosubmit(request: Request) -> dict:
    """User asks the worker to fill (and, after they approve, submit) an item."""
    from fastapi import HTTPException

    from . import apply_queue, fill_requests
    from . import ats

    if not get_settings().apply_autosubmit_enabled:
        raise HTTPException(
            status_code=403,
            detail="auto-submit is off for testers — use in-app Autofill",
        )

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    pid = int(body["posting_id"])
    apply_queue.stage(uid, pid)  # ensure it's staged + its package is ready
    pkg = apply_queue.get_package(uid, pid) or {}
    url = pkg.get("url", "")
    # Any http(s) apply URL may be attempted; formprobe decides after navigation.
    # known_ats is a confidence label for the UI (Greenhouse/Lever/Ashby hosts).
    if not ats.may_autosubmit(url):
        return {"fillable": False, "url": url, "known_ats": False}
    req = fill_requests.create(uid, pid)
    return {
        "request_id": req["id"], "status": req["status"], "fillable": True,
        "known_ats": ats.is_fillable_form(url),
    }


@app.get("/apply/request")
def apply_request_status(
    request: Request, user: str | None = None, posting_id: int = 0,
) -> dict:
    """Current fill-request state for a posting (drives the review-page polling)."""
    from . import fill_requests

    uid = _resolve_user(request, user)
    return {"request": fill_requests.for_posting(uid, posting_id)}


@app.post("/apply/request/approve")
async def apply_request_approve(request: Request) -> dict:
    """User approves a filled preview — the worker may now submit it."""
    from . import fill_requests

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    return {"ok": fill_requests.approve(uid, int(body["request_id"]))}


@app.post("/apply/request/cancel")
async def apply_request_cancel(request: Request) -> dict:
    from . import fill_requests

    body = await request.json()
    uid = _resolve_user(request, body.get("user"))
    return {"ok": fill_requests.cancel(uid, int(body["request_id"]))}


# --- worker-facing API (token-gated) -------------------------------------

@app.post("/worker/claim")
async def worker_claim(request: Request) -> dict:
    """Worker claims the next pending fill request and gets its prepared package
    (url + identity + per-question answers + resume availability)."""
    from . import apply_queue, fill_requests

    _require_apply_token(request)
    req = fill_requests.claim_next()
    if req is None:
        return {}
    pkg = apply_queue.get_package(req["user_id"], req["posting_id"]) or {}
    return {
        "request_id": req["id"], "user": req["user_id"],
        "posting_id": req["posting_id"], "url": pkg.get("url", ""),
        "identity": pkg.get("identity", {}), "questions": pkg.get("questions", []),
        "has_resume": bool(pkg.get("resume")),
    }


@app.post("/worker/claim_approved")
async def worker_claim_approved(request: Request) -> dict:
    """Worker claims the next approved request to submit it."""
    from . import fill_requests

    _require_apply_token(request)
    req = fill_requests.claim_approved()
    return {"request_id": req["id"], "user": req["user_id"],
            "posting_id": req["posting_id"]} if req else {}


@app.post("/worker/preview")
async def worker_preview(request: Request) -> dict:
    """Worker reports the filled form (screenshot + field summary) for approval."""
    from . import fill_requests

    _require_apply_token(request)
    body = await request.json()
    preview = body.get("preview") or {}
    ok = fill_requests.set_preview(int(body["request_id"]), preview)
    if ok:
        _notify_preview_ready(int(body["request_id"]), preview)
    return {"ok": ok}


def _notify_preview_ready(request_id: int, preview: dict) -> None:
    """Push the approval gate to the user's phone, so approving never requires
    opening the web page.

    Fail-open: a messaging problem must not strand the worker or lose the fill —
    the /apply review page still shows the same preview.
    """
    import logging

    try:
        from . import engine, fill_requests, push, reminders

        req = fill_requests.get(request_id)
        if req is None:
            return
        uid = req["user_id"]
        reminders.get_sender().send(uid, engine.fill_preview_message(uid, req, preview))
        # …and a push, so the phone surfaces it without Slack being open.
        push.notify_preview_ready(
            uid, engine.fill_label(uid, req),
            len(preview.get("filled") or []), len(preview.get("skipped") or []))
    except Exception:  # noqa: BLE001
        logging.getLogger("apply").exception(
            "preview notification failed; the /apply page still has it")


@app.post("/worker/result")
async def worker_result(request: Request) -> dict:
    """Worker reports the outcome of a submission (submitted | failed)."""
    from . import fill_requests, store
    from . import jobstore

    _require_apply_token(request)
    body = await request.json()
    rid = int(body["request_id"])
    if body.get("status") == "submitted":
        fill_requests.mark_submitted(rid)
        # Log it as applied + mark the posting + the queue item.
        req = fill_requests.get(rid)
        if req:
            posting = jobstore.get_posting(req["user_id"], req["posting_id"])
            if posting:
                store.create_application(
                    req["user_id"], posting["company"] or "Unknown",
                    posting["title"] or "Role", source="discovery:autosubmit")
                jobstore.mark_posting_status(posting["id"], "applied")
                from . import apply_queue
                apply_queue.mark(req["user_id"], req["posting_id"], "submitted")
        return {"ok": True}
    fill_requests.mark_failed(rid, body.get("error", "unknown error"))
    return {"ok": True}


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


@app.get("/health")
def health() -> dict:
    from .router import get_router

    s = get_settings()
    router = get_router()
    info = {
        "status": "ok",
        "router": router.name,
        "model": s.anthropic_model if s.use_llm_router else None,
        "db": s.database_path,
    }
    # Surface token usage so cost/efficiency is observable at a glance.
    if hasattr(router, "usage"):
        info["usage"] = router.usage
    from .scheduler import is_running

    info["reminder_scheduler"] = "running" if is_running() else "stopped"
    if s.slack_enabled:
        info["reminder_delivery"] = "slack"
    elif s.outbound_sms_enabled:
        info["reminder_delivery"] = "twilio"
    else:
        info["reminder_delivery"] = "log-only"
    if s.apollo_enabled:
        from . import apollo

        info["apollo"] = apollo.usage()

    from . import discovery, jobstore

    from .jobsources import directory as dir_src

    info["discovery"] = {
        "sources_enabled": s.job_sources,
        "alert_mode": s.job_alert_mode_normalized,
        "wide_rss": s.job_wide_rss_enabled,
        "wide_directory": s.job_wide_directory_enabled,
        "wide_aggregator": s.job_wide_aggregator_enabled,
        "wide_swelist": s.job_wide_swelist_enabled,
        "serpapi": s.serpapi_enabled,
        "ghost_filter": s.ghost_filter_enabled,
        "eligibility_filter": s.eligibility_filter_enabled,
        "eligibility_llm": s.eligibility_llm_enabled,
        "deck_tldr": s.deck_tldr_enabled,
        "reranker": s.reranker_enabled,
        "directory_boards": dir_src.board_count(),
        "tracked_boards": jobstore.tracked_count(),
        "poll_seconds": s.job_poll_seconds,
        "relevance_threshold": s.job_relevance_threshold,
        "last_tick": discovery.last_tick_at,
        "postings": jobstore.global_counts_by_status(),
    }
    info["auth"] = {
        "fail_open": s.auth_fail_open,
        "allowlist": bool(s.allowed_emails),
        "autosubmit": s.apply_autosubmit_enabled,
        "dev_login": s.auth_allow_dev_login,
        "sentry": bool(s.sentry_dsn.strip()),
        "llm_user_cap": s.llm_max_calls_per_user_per_day,
    }
    if s.embedding_active:
        info["embeddings"] = {
            "model": s.embedding_model,
            "calls_today": jobstore.embedding_calls_today(),
            "max_per_day": s.embedding_max_calls_per_day,
        }
    return info


def _twiml(message: str) -> Response:
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{escape(message)}</Message></Response>"
    )
    return Response(content=body, media_type="application/xml")


@app.post("/sms")
async def sms_webhook(
    request: Request,
    From: str = Form(default=""),
    Body: str = Form(default=""),
) -> Response:
    # Signature validation is optional in Phase 1; enable via env in production.
    settings = get_settings()
    if settings.twilio_validate_signature and settings.twilio_auth_token:
        from twilio.request_validator import RequestValidator

        validator = RequestValidator(settings.twilio_auth_token)
        signature = request.headers.get("X-Twilio-Signature", "")
        form = await request.form()
        url = str(request.url)
        if not validator.validate(url, dict(form), signature):
            return Response(status_code=403, content="Invalid signature")

    user_id = From or "local"
    reply = handle_sms(user_id, Body)
    return _twiml(reply)


# JSON convenience endpoint for testing without Twilio form encoding.
# Prefer POST /chat (session-authed) for the real product path.
@app.post("/message")
async def message(request: Request, payload: dict) -> dict:
    from . import auth

    if get_settings().auth_fail_open:
        user_id = payload.get("from", "local")
    else:
        user_id = auth.require_user(request)
    text = payload.get("body", "")
    reply = handle_sms(user_id, text)
    return {"reply": reply}


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


@app.get("/chat", response_class=HTMLResponse)
def chat_page() -> HTMLResponse:
    """Minimal web chat companion."""
    from . import chat_page as page

    return HTMLResponse(page.render_chat_page())


@app.get("/chat/history")
def chat_history(
    request: Request, limit: int = 100, before_id: int | None = None,
) -> dict:
    from . import auth, chat

    uid = auth.require_user(request)
    return {"user": uid, "messages": chat.history(uid, limit=limit, before_id=before_id)}


@app.post("/chat")
async def chat_send(request: Request) -> dict:
    """Send a message to the assistant (session required)."""
    from . import auth, chat

    uid = auth.require_user(request)
    body = await request.json()
    text = (body.get("text") or body.get("body") or "").strip()
    if not text:
        from fastapi import HTTPException

        raise HTTPException(status_code=400, detail="text required")
    result = chat.send(uid, text)
    return {
        "user": uid,
        "reply": result["reply"],
        "user_message": result["user_message"],
        "assistant_message": result["assistant_message"],
    }


@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    """Legacy Slack Events API webhook.

    Disabled unless ``SLACK_TRANSPORT_ENABLED=true``. In-app chat is the product
    channel now; this stays for emergency rollback only.
    """
    settings = get_settings()
    if not settings.slack_enabled:
        return Response(status_code=404, content="slack transport disabled")

    from . import slack

    raw = await request.body()

    if settings.slack_signing_secret:
        ts = request.headers.get("X-Slack-Request-Timestamp", "")
        sig = request.headers.get("X-Slack-Signature", "")
        if not slack.verify_signature(settings.slack_signing_secret, ts, raw, sig):
            return Response(status_code=403, content="invalid signature")

    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return Response(status_code=400, content="bad payload")

    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}

    background_tasks.add_task(slack.handle_event, payload)
    return Response(status_code=200)
