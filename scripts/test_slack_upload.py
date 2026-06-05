#!/usr/bin/env python3
"""Smoke-test Slack resume PDF upload using your .env credentials.

Run from the project venv:

  .venv/bin/python3 scripts/test_slack_upload.py --scopes-only
  SLACK_TEST_USER=U0AB12CD34 .venv/bin/python3 scripts/test_slack_upload.py

Get YOUR member id (not the bot id):
  Slack → Preferences → Advanced → turn on "Show member IDs"
  Your profile → ⋯ → Copy member ID  (starts with U, ~11 chars)

After adding OAuth scopes you must Reinstall App and paste the NEW xoxb- token.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_SLACK_USER_RE = re.compile(r"^U[A-Z0-9]{8,}$")
_SLACK_CHANNEL_RE = re.compile(r"^[CDG][A-Z0-9]{8,}$")
_PLACEHOLDER_HINTS = (
    "YOURREALID",
    "0123456789",
    "XXXXXXXX",
    "EXAMPLE",
    "YOUR_",
)


def _valid_slack_id(value: str, *, kind: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    upper = value.upper()
    if any(h in upper for h in _PLACEHOLDER_HINTS):
        return ""
    if kind == "user" and _SLACK_USER_RE.match(value):
        return value
    if kind == "channel" and _SLACK_CHANNEL_RE.match(value):
        return value
    return ""


def _check_scopes(token: str) -> bool:
    import httpx

    auth = httpx.post(
        "https://slack.com/api/auth.test",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    ).json()
    if not auth.get("ok"):
        print(f"ERROR: auth.test failed: {auth.get('error')}")
        print("Reinstall the Slack app and update SLACK_BOT_TOKEN in .env")
        return False

    print(f"Token OK — bot {auth.get('user')} @ {auth.get('team')}")
    print(f"  bot_id={auth.get('bot_id')}  (this is NOT your member id)")

    upload = httpx.post(
        "https://slack.com/api/files.getUploadURLExternal",
        headers={"Authorization": f"Bearer {token}"},
        data={"filename": "scope_check.pdf", "length": "4"},
        timeout=10.0,
    ).json()
    if upload.get("ok"):
        print("files:write OK — files.getUploadURLExternal succeeded")
    else:
        print(f"files:write FAILED — {upload.get('error')}")
        print("Add files:write → Reinstall App → copy new xoxb- token → update .env")
        return False

    msg = httpx.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {token}"},
        json={"channel": auth.get("user_id"), "text": "scope check (ignore)"},
        timeout=10.0,
    ).json()
    # posting to self may fail; probe with a nonsense channel instead
    if not msg.get("ok") and msg.get("error") == "missing_scope":
        print("chat:write MISSING — add scope and reinstall app")
        return False
    print("chat:write OK (token can call chat.postMessage)")
    return True


def _resolve_dm_target(token: str, user: str, channel: str) -> str | None:
    """Return a channel id (D/C/...) or user id (U) to send to."""
    from app import slack

    if channel:
        return channel
    if not user:
        return None

    # Prefer opening a DM — works once im:write is granted.
    dm = slack.open_dm_channel(token, user)
    if dm:
        print(f"Opened DM channel: {dm}")
        return dm

    print("conversations.open failed — trying chat.postMessage with user id directly")
    print("(If this fails, add im:write scope, reinstall app, DM the bot once, retry)")
    return user


def main() -> int:
    parser = argparse.ArgumentParser(description="Test Slack resume PDF upload")
    parser.add_argument(
        "--scopes-only",
        action="store_true",
        help="Only verify token + files:write (no user id needed)",
    )
    args = parser.parse_args()

    try:
        import httpx  # noqa: F401
    except ImportError:
        print("ERROR: use the project venv:")
        print("  .venv/bin/python3 scripts/test_slack_upload.py --scopes-only")
        return 1

    from app.config import get_settings

    settings = get_settings()
    token = settings.slack_bot_token.strip()
    if not token:
        print("ERROR: SLACK_BOT_TOKEN not set in .env")
        return 1

    if args.scopes_only:
        return 0 if _check_scopes(token) else 1

    raw_user = os.environ.get("SLACK_TEST_USER", "").strip()
    raw_channel = os.environ.get("SLACK_TEST_CHANNEL", "").strip()
    channel = _valid_slack_id(raw_channel, kind="channel")
    user = _valid_slack_id(raw_user, kind="user")
    if not user:
        user = _valid_slack_id(os.environ.get("JOB_ALERT_USER", ""), kind="user")

    if raw_user and not user:
        print(f"ERROR: SLACK_TEST_USER={raw_user!r} is not a valid member id.")
        print("Use Copy member ID from YOUR profile — not the bot id, not an example.")
        return 1

    if not _check_scopes(token):
        return 1

    if not channel and not user:
        print()
        print("Scopes look good. Now test the full upload with YOUR member id:")
        print("  SLACK_TEST_USER=U0xxxxxxxxx .venv/bin/python3 scripts/test_slack_upload.py")
        print()
        print("Tip: run --scopes-only anytime to verify the token without a user id.")
        return 1

    target = _resolve_dm_target(token, user, channel)
    if not target:
        return 1

    sample = ROOT / "resumes" / "Resume_SWE (7).pdf"
    if not sample.is_file():
        sample = next(ROOT.glob("resumes/*.pdf"), None)
    if not sample or not sample.is_file():
        print("ERROR: no sample PDF in resumes/")
        return 1

    pdf_bytes = sample.read_bytes()
    print(f"Uploading {sample.name} ({len(pdf_bytes)} bytes) → {target} ...")

    import httpx
    from app import slack

    if not slack.post_message(
        token,
        target,
        "🧪 Resume upload smoke test — PDF attached below.",
    ):
        print("ERROR: chat.postMessage failed.")
        print("Try: DM your bot once, then rerun. Or add im:write + reinstall app.")
        return 1

    if not slack.upload_file(
        token,
        target,
        "Resume_smoke_test.pdf",
        pdf_bytes,
        comment="Smoke test: resume PDF attachment",
    ):
        print("ERROR: file upload failed — see logs above for Slack error code.")
        return 1

    print("OK — check Slack for the test message and PDF.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
