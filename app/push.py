"""Push notifications to the iPhone app (APNs).

The difference between a tool you remember to check and one that tells you: a match
lands when new matches arrive, and the phone
says so. Everything else in the pipeline already knows when those happen; this is
just the delivery.

Shaped like every other paid/external integration here:

* **Gated.** Off unless `PUSH_ENABLED` plus the four APNs settings are present.
* **Fail-open.** A missing key, an expired token, APNs being down — none of it may
  break the thing that triggered the notification. `send` returns a count and
  swallows everything else.
* **Offline-safe.** The signing and HTTP libraries are imported lazily, so the
  suite runs (and the app boots) without `cryptography` / `h2` installed at all.

Tokens are registered by the app itself (`POST /apply/device`); a token that APNs
rejects as gone is dropped, so a reinstalled app doesn't accumulate dead rows.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from .config import get_settings
from .db import connect

logger = logging.getLogger("push")

# APNs hosts. Sandbox is what a development build (Xcode / free signing) talks to;
# TestFlight and App Store builds use production. Wrong host = "BadDeviceToken".
_HOST_PROD = "https://api.push.apple.com"
_HOST_SANDBOX = "https://api.sandbox.push.apple.com"

# APNs rejects a token that's no longer valid — the app was deleted, or the token
# was minted for the other environment. Both mean: stop sending to it.
_DEAD_TOKEN_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- device registry --------------------------------------------------------

def register_device(user_id: str, token: str, platform: str = "ios",
                    timezone: str | None = None) -> bool:
    """Record a device token. Idempotent — re-registering refreshes it rather than
    duplicating, since iOS hands the app the same token on every launch."""
    token = (token or "").strip()
    if not user_id or not token:
        return False
    tz = (timezone or "").strip() or None
    with connect() as conn:
        conn.execute(
            "INSERT INTO device_tokens (user_id, token, platform, timezone, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, token) DO UPDATE SET "
            "updated_at = excluded.updated_at, "
            "timezone = COALESCE(excluded.timezone, device_tokens.timezone)",
            (user_id, token, platform or "ios", tz, _now(), _now()),
        )
    return True


def forget_device(user_id: str, token: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM device_tokens WHERE user_id = ? AND token = ?",
            (user_id, token))
        return cur.rowcount > 0


def devices(user_id: str) -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT token FROM device_tokens WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,)).fetchall()
    return [r["token"] for r in rows]


# --- sending ----------------------------------------------------------------

def configured() -> bool:
    """True when push is switched on *and* fully configured. Everything below is a
    no-op until this is true, so the feature can ship dark."""
    s = get_settings()
    return bool(
        getattr(s, "push_enabled", False)
        and s.apns_key_id and s.apns_team_id and s.apns_bundle_id
        and s.apns_key_source
    )


def send(user_id: str, title: str, body: str, *, data: dict | None = None) -> int:
    """Notify every device registered to ``user_id``. Returns how many were sent.

    Never raises: the caller is always in the middle of something more important
    (recording a preview, sending a digest) and a notification is the least of it.
    """
    if not configured():
        return 0
    tokens = devices(user_id)
    if not tokens:
        return 0
    try:
        auth = _auth_header()
    except Exception:  # noqa: BLE001 — bad/missing key, or crypto libs absent
        logger.warning("push: could not build the APNs token; skipping", exc_info=True)
        return 0

    payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}
    if data:
        payload.update(data)

    sent = 0
    for token in tokens:
        try:
            if _post(token, payload, auth):
                sent += 1
        except Exception:  # noqa: BLE001 — one bad device never blocks the others
            logger.warning("push: delivery failed for one device", exc_info=True)
    return sent


def _post(token: str, payload: dict, auth: str) -> bool:
    """One APNs delivery. Returns True on 200. Drops the token if APNs says it's
    dead, so a reinstalled app doesn't leave rows we keep retrying forever."""
    import httpx  # local: keeps import cost off the app's startup path

    s = get_settings()
    host = _HOST_SANDBOX if getattr(s, "apns_use_sandbox", False) else _HOST_PROD
    # APNs is HTTP/2 only; httpx needs the `h2` extra for that.
    with httpx.Client(http2=True, timeout=10) as client:
        r = client.post(
            f"{host}/3/device/{token}",
            headers={"authorization": auth, "apns-topic": s.apns_bundle_id,
                     "apns-push-type": "alert", "apns-priority": "10"},
            content=json.dumps(payload).encode(),
        )
    if r.status_code == 200:
        return True
    reason = ""
    try:
        reason = (r.json() or {}).get("reason", "")
    except Exception:  # noqa: BLE001
        pass
    if reason in _DEAD_TOKEN_REASONS:
        logger.info("push: dropping dead device token (%s)", reason)
        with connect() as conn:
            conn.execute("DELETE FROM device_tokens WHERE token = ?", (token,))
    else:
        logger.warning("push: APNs %s %s", r.status_code, reason)
    return False


_token_cache: tuple[str, float] | None = None


def _signing_key(s) -> str:
    """The .p8 contents, from a file or straight out of the environment.

    A secret is a better home for a signing key than a file on a volume, and it
    is one command rather than an SSH copy. `fly secrets set` folds the literal
    "\\n" when a shell escapes it, which produces a PEM that looks right and
    fails inside PyJWT with an unhelpful error, so unfold it here.
    """
    if s.apns_key_path.strip():
        with open(s.apns_key_path, "r", encoding="utf-8") as fh:
            return fh.read()
    return s.apns_key_pem.replace("\\n", "\n").strip() + "\n"


def _auth_header() -> str:
    """A signed APNs bearer token, cached.

    APNs requires an ES256 JWT signed with the .p8 key, and *rejects tokens younger
    than an hour being regenerated* — so this must be cached, not rebuilt per send.
    """
    global _token_cache
    if _token_cache is not None and time.time() - _token_cache[1] < 2400:  # 40 min
        return _token_cache[0]

    import jwt  # lazy: PyJWT + cryptography are only needed when push is on

    s = get_settings()
    key = _signing_key(s)
    token = jwt.encode(
        {"iss": s.apns_team_id, "iat": int(time.time())},
        key, algorithm="ES256", headers={"kid": s.apns_key_id},
    )
    _token_cache = (f"bearer {token}", time.time())
    return _token_cache[0]


def reset_for_tests() -> None:
    global _token_cache
    _token_cache = None


# --- push triggers ---------------------------------------------------------

def notify_new_matches(user_id: str, count: int, top: str | None = None) -> int:
    """New job matches landed."""
    if count <= 0:
        return 0
    from . import voice

    title, body = voice.match_notification(user_id, count, top)
    return send(user_id, title, body, data={"kind": "matches", "count": count})
