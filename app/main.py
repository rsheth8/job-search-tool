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

# Ensure the schema exists as soon as the app module is imported, regardless of
# how it's launched (uvicorn, TestClient, etc.). init_db() is idempotent.
init_db()


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


@app.post("/apply/mark")
async def apply_mark(request: Request) -> dict:
    from . import apply_queue
    from . import dashboard as dash

    body = await request.json()
    uid = body.get("user") or dash.default_user()
    return {"ok": apply_queue.mark(uid, int(body["posting_id"]), body.get("status", ""))}


@app.post("/apply/remove")
async def apply_remove(request: Request) -> dict:
    from . import apply_queue
    from . import dashboard as dash

    body = await request.json()
    uid = body.get("user") or dash.default_user()
    return {"ok": apply_queue.remove(uid, int(body["posting_id"]))}


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
