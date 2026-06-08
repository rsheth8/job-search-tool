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
