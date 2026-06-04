"""Slack transport — Events API inbound + Web API outbound.

This mirrors the Twilio seam exactly, so the *brain* never changes:

  - Inbound: Slack POSTs events to ``/slack/events`` (app/main.py). We verify the
    signature, then hand the message text to ``engine.handle_sms`` — the same
    function Twilio's ``/sms`` calls — and post the reply back with the Web API.
  - Outbound: ``SlackSender`` implements ``reminders.Sender`` (one ``send``
    method), so scheduled reminders ship over Slack with no change to
    ``deliver_due_reminders``.

Why Slack over SMS here: no A2P 10DLC approval, free, outbound works
immediately, and the dashboard's markdown/emoji actually render. Twilio stays in
the tree, dormant — a config flip away.

Design notes:
  - **Import-light.** Uses ``httpx`` (already a dependency) for the two Web API
    calls instead of pulling in ``slack_sdk``. Network/engine imports are lazy so
    ``import app.slack`` is cheap and test-safe.
  - **user_id = Slack user ID.** That's the stable per-user key for the DB, and
    ``chat.postMessage(channel=<user id>)`` opens a DM, so reminders reach the
    user with no extra mapping (parallels Twilio using the phone number).
  - Replies to inbound go to the *event's* channel (a DM channel, or a public
    channel for an @-mention); reminders always DM the user.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time

logger = logging.getLogger("slack")

_API_POST_MESSAGE = "https://slack.com/api/chat.postMessage"

# Replay window for inbound request signatures (Slack's own recommendation).
_MAX_SIGNATURE_AGE_S = 60 * 5

# Tiny in-memory dedupe for redelivered events. Slack retries an event up to a
# few times if we're slow to ack; we ack fast (background task) but also guard
# here so a retry never double-logs. A single-user tool doesn't need anything
# durable; the cap keeps it from growing without bound.
_seen_event_ids: set[str] = set()
_SEEN_CAP = 1000

# Strips a leading bot mention from app_mention text: "<@U123> applied stripe".
_LEADING_MENTION = re.compile(r"^\s*<@[A-Z0-9]+>\s*")


def verify_signature(
    signing_secret: str, timestamp: str, body: bytes, signature: str
) -> bool:
    """Validate Slack's ``X-Slack-Signature`` over the raw request body.

    Slack signs ``v0:<timestamp>:<raw body>`` with HMAC-SHA256. We also reject
    stale timestamps to blunt replay. Returns False (never raises) on any
    malformed input so the caller can simply 403.
    """
    if not (timestamp and signature):
        return False
    try:
        if abs(time.time() - int(timestamp)) > _MAX_SIGNATURE_AGE_S:
            return False
    except ValueError:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    digest = hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={digest}", signature)


def _mark_seen(event_id: str) -> bool:
    """Record an event id; return True if it's new (first time we've seen it)."""
    if event_id in _seen_event_ids:
        return False
    if len(_seen_event_ids) >= _SEEN_CAP:
        _seen_event_ids.clear()  # cheap, good enough for a personal tool
    _seen_event_ids.add(event_id)
    return True


def post_message(token: str, channel: str, text: str) -> None:
    """Post ``text`` to ``channel`` via the Slack Web API. Never raises.

    A failure is logged and swallowed so one bad send can't take down the
    request handler or the reminder batch — same contract as the rest of the
    delivery path (``deliver_due_reminders`` leaves failed rows pending).
    """
    import httpx

    try:
        resp = httpx.post(
            _API_POST_MESSAGE,
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "text": text},
            timeout=10.0,
        )
        data = resp.json()
    except Exception:  # noqa: BLE001 — network/JSON errors must not propagate
        logger.exception("slack chat.postMessage request failed")
        return
    if not data.get("ok"):
        logger.warning("slack chat.postMessage error: %s", data.get("error"))


class SlackSender:
    """Outbound reminder delivery over Slack (implements ``reminders.Sender``).

    ``user_id`` is the Slack user ID; posting to it as a channel opens a DM.
    """

    def __init__(self, bot_token: str) -> None:
        self._token = bot_token

    def send(self, user_id: str, body: str) -> None:
        post_message(self._token, user_id, body)
        logger.info("[reminder→%s via slack] %s", user_id, body)


def handle_event(payload: dict) -> None:
    """Process one Slack Events API callback: run the brain, post the reply.

    Called from a background task after the webhook has already 200'd, so Slack
    never retries us for being slow. We only act on real user messages — bot
    posts, edits/joins (``subtype``), and our own messages are ignored to avoid
    loops, and redelivered events are deduped by ``event_id``.
    """
    if payload.get("type") != "event_callback":
        return
    event = payload.get("event", {})
    if event.get("type") not in ("message", "app_mention"):
        return
    # Skip bot/system/edited messages — these are how feedback loops start.
    if event.get("bot_id") or event.get("subtype"):
        return

    text = (event.get("text") or "").strip()
    user = event.get("user") or ""
    channel = event.get("channel") or ""
    if not (text and user and channel):
        return

    event_id = payload.get("event_id")
    if event_id and not _mark_seen(event_id):
        return  # duplicate redelivery

    text = _LEADING_MENTION.sub("", text)  # drop "<@bot>" from @-mentions

    from .config import get_settings
    from .engine import handle_sms

    reply = handle_sms(user, text)
    token = get_settings().slack_bot_token
    if token:
        post_message(token, channel, reply)
