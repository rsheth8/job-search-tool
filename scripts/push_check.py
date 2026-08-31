#!/usr/bin/env python3
"""Diagnose push notifications, and optionally send one real test alert.

Push is the one beta feature that cannot be verified from outside the machine.
`/health.push` says whether the settings are *present*; it cannot say whether the
signing key actually signs, whether the bundle id matches the build, or whether
the token in the database is for the environment we're sending to. Those are
exactly the failures people hit, and every one of them is silent: `send()` is
fail-open by design, so a misconfigured deployment looks identical to a working
one that nobody happened to notify.

This runs *on the machine*, where the secrets and the device tokens both live:

    fly ssh console -a job-search-tool -C "cd /app && python -m scripts.push_check --list"
    fly ssh console -a job-search-tool -C "cd /app && python -m scripts.push_check --user usr_abc --check"
    fly ssh console -a job-search-tool -C "cd /app && python -m scripts.push_check --user usr_abc"

`--check` inspects and stops. Without it, one real notification is sent to every
device registered to that user, and the APNs reply for each is printed — including
the reason string, which is the only thing that distinguishes the failure modes
from each other.

Exit code is 0 when the thing you asked for worked, 1 otherwise, so this is usable
as a deploy gate.
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import sys

from app import push
from app.config import get_settings
from app.db import connect

# APNs reason strings we can say something more useful about than the string.
_HINTS = {
    "BadDeviceToken": (
        "the token was minted for the *other* APNs environment. A TestFlight or "
        "App Store build needs APNS_USE_SANDBOX=false; an Xcode build needs true."
    ),
    "DeviceTokenNotForTopic": (
        "APNS_BUNDLE_ID does not match the bundle id of the build that registered "
        "this token."
    ),
    "TopicDisallowed": "APNS_BUNDLE_ID is not a topic this key is allowed to send to.",
    "InvalidProviderToken": (
        "the signing key, APNS_KEY_ID and APNS_TEAM_ID do not agree — a key id "
        "that doesn't match the .p8 is the usual cause."
    ),
    "ExpiredProviderToken": "the cached bearer token aged out; restarting the app fixes it.",
    "Unregistered": "the app was deleted from this device.",
}


def users_with_devices() -> list[tuple[str, int]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT user_id, COUNT(*) AS n FROM device_tokens "
            "GROUP BY user_id ORDER BY n DESC, user_id"
        ).fetchall()
    return [(r["user_id"], r["n"]) for r in rows]


def _have(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def diagnose(user_id: str | None = None) -> dict:
    """Everything knowable without talking to Apple."""
    s = get_settings()
    blockers: list[str] = []

    if not s.push_enabled:
        blockers.append("PUSH_ENABLED is not true — push is switched off")
    for name, value in (("APNS_KEY_ID", s.apns_key_id),
                        ("APNS_TEAM_ID", s.apns_team_id),
                        ("APNS_BUNDLE_ID", s.apns_bundle_id)):
        if not value.strip():
            blockers.append(f"{name} is not set")
    if not s.apns_key_source:
        blockers.append("no signing key: set APNS_KEY_PEM (or APNS_KEY_PATH)")

    # The libraries are lazy imports, so a missing one only shows up at send time.
    for mod, label in (("jwt", "pyjwt"), ("cryptography", "cryptography"),
                       ("h2", "h2 (httpx[http2])")):
        if not _have(mod):
            blockers.append(f"{label} is not installed — APNs cannot be reached")

    tokens = push.devices(user_id) if user_id else []
    return {
        "enabled": s.push_enabled,
        "key_source": s.apns_key_source,
        "sandbox": s.apns_use_sandbox,
        "host": push._HOST_SANDBOX if s.apns_use_sandbox else push._HOST_PROD,
        "bundle_id": s.apns_bundle_id,
        "key_id": s.apns_key_id,
        "team_id": s.apns_team_id,
        "configured": push.configured(),
        "tokens": tokens,
        "blockers": blockers,
    }


def _mask(token: str) -> str:
    return token if len(token) <= 12 else f"{token[:6]}…{token[-4:]}"


class _Capture(logging.Handler):
    """`send` is fail-open: it logs and returns a count. The log line is the only
    place the APNs reason survives, so read it rather than re-implementing the
    request."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


def send_test(user_id: str, title: str, body: str) -> tuple[int, list[str]]:
    """Send one notification and return (delivered, log lines)."""
    cap = _Capture()
    push.logger.addHandler(cap)
    previous = push.logger.level
    push.logger.setLevel(logging.DEBUG)
    try:
        sent = push.send(user_id, title, body, data={"kind": "push_check"})
    finally:
        push.logger.removeHandler(cap)
        push.logger.setLevel(previous)
    return sent, cap.records


def report(info: dict) -> None:
    print("Push configuration")
    print(f"  PUSH_ENABLED      {info['enabled']}")
    print(f"  signing key       {info['key_source'] or '(none)'}")
    print(f"  APNS_KEY_ID       {info['key_id'] or '(unset)'}")
    print(f"  APNS_TEAM_ID      {info['team_id'] or '(unset)'}")
    print(f"  APNS_BUNDLE_ID    {info['bundle_id'] or '(unset)'}")
    print(f"  environment       {'sandbox' if info['sandbox'] else 'production'}"
          f"  ({info['host']})")
    print(f"  configured()      {info['configured']}")
    for line in info["blockers"]:
        print(f"  [blocked] {line}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user", help="user id to notify")
    ap.add_argument("--list", action="store_true",
                    help="list users with a registered device, and stop")
    ap.add_argument("--check", action="store_true",
                    help="inspect the configuration; send nothing")
    ap.add_argument("--title", default="JobPilot")
    ap.add_argument("--body", default="Push is working. This is a test.")
    args = ap.parse_args()

    if args.list:
        rows = users_with_devices()
        if not rows:
            print("No devices registered. Open the app and allow notifications "
                  "once — the token is registered on launch.")
            return 1
        for uid, n in rows:
            print(f"{uid}\t{n} device(s)")
        return 0

    info = diagnose(args.user)
    report(info)

    if not args.user:
        print("\nPass --user to check a specific account's devices, or --list.")
        return 1 if info["blockers"] else 0

    print(f"\nDevices for {args.user}: {len(info['tokens'])}")
    for t in info["tokens"]:
        print(f"  {_mask(t)}")
    if not info["tokens"]:
        print("  none — open the app on the device and allow notifications.")

    if args.check:
        return 1 if (info["blockers"] or not info["tokens"]) else 0
    if info["blockers"]:
        print("\nNot sending: fix the blockers above first.")
        return 1
    if not info["tokens"]:
        return 1

    sent, lines = send_test(args.user, args.title, args.body)
    print(f"\nDelivered to {sent} of {len(info['tokens'])} device(s).")
    for line in lines:
        print(f"  {line}")
        for reason, hint in _HINTS.items():
            if reason in line:
                print(f"    → {hint}")
    if sent == 0:
        print("\nNothing was delivered. If no APNs reply is shown above, the "
              "bearer token could not be signed — check the .p8 contents.")
    return 0 if sent else 1


if __name__ == "__main__":
    sys.exit(main())
