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
    from .scheduler import start_scheduler

    start_scheduler()
    yield


app = FastAPI(
    title="Job Search SMS Intelligence", version="0.1.0", lifespan=lifespan
)

# CORS for the application-autofill extension: its content script runs on ATS
# origins (greenhouse.io, lever.co, …) and calls /apply/* cross-origin. Writes are
# additionally gated by the X-Apply-Token header (see _require_apply_token).
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

_cors = [o.strip() for o in get_settings().apply_cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors or ["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Ensure the schema exists as soon as the app module is imported, regardless of
# how it's launched (uvicorn, TestClient, etc.). init_db() is idempotent.
init_db()


def _require_apply_token(request: Request) -> None:
    """Gate the autofill endpoints when APPLY_API_TOKEN is configured. No-op when
    it's blank (local/dev). Raises 401 on a missing/wrong token."""
    from fastapi import HTTPException

    expected = get_settings().apply_api_token.strip()
    if not expected:
        return
    if request.headers.get("X-Apply-Token", "").strip() != expected:
        raise HTTPException(status_code=401, detail="bad or missing X-Apply-Token")


@app.get("/", response_class=HTMLResponse)
def dashboard(user: str | None = None) -> HTMLResponse:
    """Read-only pipeline dashboard. ``?user=`` overrides the default user."""
    from . import dashboard as dash

    return HTMLResponse(dash.render(user))


@app.get("/train", response_class=HTMLResponse)
def train_page(user: str | None = None) -> HTMLResponse:
    """Tinder-style swipe trainer to bootstrap the re-ranker on real postings."""
    from . import dashboard as dash
    from . import trainer

    return HTMLResponse(trainer.render_page(user or dash.default_user()))


@app.get("/train/deck")
def train_deck(
    user: str | None = None, n: int = 15, diverse: bool = False,
    mode: str | None = None,
) -> dict:
    """Return scored cards FAST (no summaries) so the deck shows instantly; the
    client fetches summaries lazily via /train/summaries.

    ``mode`` picks the deck strategy: 'best' (top matches), 'mix' (spread for
    balanced training), or 'learn' (active learning — most-uncertain first).
    ``diverse`` is kept for back-compat (== mode 'mix')."""
    from . import dashboard as dash
    from . import trainer

    uid = user or dash.default_user()
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
    from . import dashboard as dash
    from . import insights, profile as prof

    body = await request.json()
    uid = body.get("user") or dash.default_user()
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
    from . import dashboard as dash
    from . import profile as prof
    from . import reranker, trainer

    body = await request.json()
    uid = body.get("user") or dash.default_user()
    item = body.get("item") or {}
    label = body.get("label", "pass")
    trainer.record_label(uid, item, label)
    reranker.maybe_retrain(uid, prof.get_profile(uid))
    return trainer.stats(uid)


@app.get("/apply")
def apply_page(user: str | None = None) -> HTMLResponse:
    """Semi-auto application queue: staged matches with pre-assembled packages."""
    from . import apply_queue
    from . import dashboard as dash

    return HTMLResponse(apply_queue.render_page(user or dash.default_user()))


@app.get("/apply/data")
def apply_data(user: str | None = None) -> dict:
    """The apply queue plus the top un-staged matches available to stage."""
    from . import apply_queue, jobstore
    from . import dashboard as dash

    uid = user or dash.default_user()
    staged = {it["posting_id"] for it in apply_queue.list_queue(uid)}
    queued = [
        {"posting_id": r["id"], "company": r["company"], "title": r["title"],
         "url": r["url"], "score": r["relevance_score"], "source": r["source"]}
        for r in jobstore.list_review_queue(uid) if r["id"] not in staged
    ]
    return {"user": uid, "queued": queued, "queue": apply_queue.list_queue(uid)}


@app.post("/apply/stage")
async def apply_stage(request: Request) -> dict:
    from . import apply_queue
    from . import dashboard as dash

    body = await request.json()
    uid = body.get("user") or dash.default_user()
    return {"ok": apply_queue.stage(uid, int(body["posting_id"]))}


@app.post("/apply/package")
async def apply_package(request: Request) -> dict:
    """Assemble (and cache) the full application package for one staged item."""
    from . import apply_queue
    from . import dashboard as dash

    body = await request.json()
    uid = body.get("user") or dash.default_user()
    pkg = apply_queue.get_package(uid, int(body["posting_id"]))
    return pkg or {"error": "not found"}


@app.post("/apply/answer/save")
async def apply_answer_save(request: Request) -> dict:
    """Persist a user-edited answer to one of an item's questions (by index)."""
    from . import apply_queue
    from . import dashboard as dash

    body = await request.json()
    uid = body.get("user") or dash.default_user()
    ok = apply_queue.save_answer(
        uid, int(body["posting_id"]), int(body.get("index", 0)), body.get("answer", "")
    )
    return {"ok": ok}


@app.post("/apply/answer/redraft")
async def apply_answer_redraft(request: Request) -> dict:
    """Regenerate a fresh answer for one of an item's questions (by index)."""
    from . import apply_queue
    from . import dashboard as dash

    body = await request.json()
    uid = body.get("user") or dash.default_user()
    answer = apply_queue.redraft_answer(
        uid, int(body["posting_id"]), int(body.get("index", 0))
    )
    return {"answer": answer} if answer is not None else {"error": "not found"}


@app.post("/apply/mark")
async def apply_mark(request: Request) -> dict:
    from . import apply_queue
    from . import dashboard as dash

    body = await request.json()
    uid = body.get("user") or dash.default_user()
    return {"ok": apply_queue.mark(uid, int(body["posting_id"]), body.get("status", ""))}


@app.post("/apply/applied")
async def apply_applied(request: Request) -> dict:
    """Mobile app: the user finished and submitted an application in the in-app
    browser. Log it (application record + posting + queue), like the worker does."""
    from . import apply_queue, jobstore, store
    from . import dashboard as dash

    body = await request.json()
    uid = body.get("user") or dash.default_user()
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
    from . import dashboard as dash

    body = await request.json()
    uid = body.get("user") or dash.default_user()
    return {"ok": apply_queue.remove(uid, int(body["posting_id"]))}


@app.get("/apply/rules")
def apply_rules(request: Request) -> dict:
    """The field-matching rules, so the extension and the iOS browser stop carrying
    hand-ported copies that drift out of date (see fieldmatch.rules_payload)."""
    from . import fieldmatch

    _require_apply_token(request)
    return fieldmatch.rules_payload()


@app.get("/apply/identity")
def apply_identity(request: Request, user: str | None = None) -> dict:
    """The applicant identity map the extension paints onto simple form fields."""
    from . import applicant
    from . import dashboard as dash

    _require_apply_token(request)
    uid = user or dash.default_user()
    return {"user": uid, "fields": applicant.autofill_map(uid)}


@app.post("/apply/identity")
async def apply_identity_set(request: Request) -> dict:
    """Save/update applicant identity (used by the extension options page)."""
    from . import applicant
    from . import dashboard as dash

    _require_apply_token(request)
    body = await request.json()
    uid = body.get("user") or dash.default_user()
    fields = body.get("fields") or {}
    return {"user": uid, "fields": applicant.get_identity(uid),
            "saved": applicant.set_identity(uid, fields)}


@app.post("/apply/answer")
async def apply_answer(request: Request) -> dict:
    """Draft an answer to one free-text application question. ``posting_id`` adds
    the JD/company as context; ``company``/``title``/``jd`` can be passed directly
    when the extension only has the live page (no posting on file)."""
    from . import applicant, apply_queue, jobstore, outreach
    from . import dashboard as dash
    from . import profile as prof

    _require_apply_token(request)
    body = await request.json()
    uid = body.get("user") or dash.default_user()
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
    from . import apply_queue, fill_requests
    from . import dashboard as dash

    from . import ats

    body = await request.json()
    uid = body.get("user") or dash.default_user()
    pid = int(body["posting_id"])
    apply_queue.stage(uid, pid)  # ensure it's staged + its package is ready
    pkg = apply_queue.get_package(uid, pid) or {}
    url = pkg.get("url", "")
    # Only first-party ATS forms (Greenhouse/Lever/Ashby) are auto-fillable. For
    # aggregator/login/captcha links there's no form to fill — hand off to desktop.
    if not ats.is_fillable_form(url):
        return {"fillable": False, "url": url}
    req = fill_requests.create(uid, pid)
    return {"request_id": req["id"], "status": req["status"], "fillable": True}


@app.get("/apply/request")
def apply_request_status(user: str | None = None, posting_id: int = 0) -> dict:
    """Current fill-request state for a posting (drives the review-page polling)."""
    from . import fill_requests
    from . import dashboard as dash

    uid = user or dash.default_user()
    return {"request": fill_requests.for_posting(uid, posting_id)}


@app.post("/apply/request/approve")
async def apply_request_approve(request: Request) -> dict:
    """User approves a filled preview — the worker may now submit it."""
    from . import fill_requests
    from . import dashboard as dash

    body = await request.json()
    uid = body.get("user") or dash.default_user()
    return {"ok": fill_requests.approve(uid, int(body["request_id"]))}


@app.post("/apply/request/cancel")
async def apply_request_cancel(request: Request) -> dict:
    from . import fill_requests
    from . import dashboard as dash

    body = await request.json()
    uid = body.get("user") or dash.default_user()
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
        from . import engine, fill_requests, reminders

        req = fill_requests.get(request_id)
        if req is None:
            return
        reminders.get_sender().send(
            req["user_id"], engine.fill_preview_message(req["user_id"], req, preview))
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
def apply_resume(user: str | None = None, id: int = 0) -> Response:
    """Download the staged item's tailored resume PDF (rebuilt from cache)."""
    from . import apply_queue
    from . import dashboard as dash

    uid = user or dash.default_user()
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
@app.post("/message")
async def message(payload: dict) -> dict:
    user_id = payload.get("from", "local")
    text = payload.get("body", "")
    reply = handle_sms(user_id, text)
    return {"reply": reply}


@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks):
    """Slack Events API webhook (primary transport).

    Three jobs: answer Slack's one-time URL-verification challenge, verify the
    request signature, and dispatch real message events to the engine. We ack
    within Slack's 3s window by handing the work to a background task — the brain
    (and any LLM call) runs after we've already returned 200, so Slack never
    retries us for being slow.
    """
    from . import slack

    raw = await request.body()
    settings = get_settings()

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
