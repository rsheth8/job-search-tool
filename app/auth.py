"""Sign in with Apple + opaque session tokens.

Identity flow:
  1. iOS / web obtains an Apple identity token (JWT).
  2. ``POST /auth/apple`` verifies it against Apple's JWKS, upserts a user row,
     and returns a long-lived session token.
  3. Clients send ``Authorization: Bearer <session>`` on chat + apply calls.

Sessions are opaque random tokens stored hashed in SQLite (so a DB leak isn't
enough to impersonate). The plaintext token is shown once at login.

Dev-only: when ``AUTH_ALLOW_DEV_LOGIN`` is on, ``POST /auth/dev`` mints a
session without Apple — used by the test suite and local CLI.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request

from .config import get_settings
from .db import connect

logger = logging.getLogger("auth")

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"

# Cached JWKS client (PyJWT). Lazily built so tests that never hit Apple stay
# offline.
_jwks_client = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_user_id() -> str:
    """Stable opaque app user id (not an Apple sub — those stay in apple_sub)."""
    return "usr_" + uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# Apple identity token
# ---------------------------------------------------------------------------

def _audiences() -> list[str]:
    s = get_settings()
    out: list[str] = []
    for raw in (s.apple_client_ids or "").split(","):
        v = raw.strip()
        if v and v not in out:
            out.append(v)
    # Bundle id is the native audience even if the env list is empty — keeps
    # local/dev usable once entitlements are on.
    if s.apns_bundle_id.strip() and s.apns_bundle_id.strip() not in out:
        out.append(s.apns_bundle_id.strip())
    return out


def verify_apple_identity_token(identity_token: str) -> dict[str, Any]:
    """Verify an Apple identity token; return claims (must include ``sub``).

    Raises ``HTTPException(401)`` on any validation failure.
    """
    import jwt
    from jwt import PyJWKClient

    global _jwks_client
    audiences = _audiences()
    if not audiences:
        raise HTTPException(
            status_code=503,
            detail="Apple Sign In is not configured (set APPLE_CLIENT_IDS)",
        )
    try:
        if _jwks_client is None:
            _jwks_client = PyJWKClient(APPLE_JWKS_URL, cache_keys=True)
        key = _jwks_client.get_signing_key_from_jwt(identity_token)
        claims = jwt.decode(
            identity_token,
            key.key,
            algorithms=["RS256"],
            audience=audiences if len(audiences) > 1 else audiences[0],
            issuer=APPLE_ISSUER,
            options={"verify_aud": True},
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — surface as 401, never 500 for bad tokens
        logger.info("apple token verify failed: %s", exc)
        raise HTTPException(status_code=401, detail="invalid Apple identity token") from exc

    if not claims.get("sub"):
        raise HTTPException(status_code=401, detail="Apple token missing sub")
    return claims


# ---------------------------------------------------------------------------
# Users + sessions
# ---------------------------------------------------------------------------

def get_user(user_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def get_user_by_apple_sub(apple_sub: str) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE apple_sub = ?", (apple_sub,)
        ).fetchone()
    return dict(row) if row else None


def _insert_user(
    *,
    user_id: str,
    apple_sub: str | None,
    email: str | None,
    display_name: str | None,
) -> dict:
    now = _iso(_utcnow())
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (id, apple_sub, email, display_name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, apple_sub, email, display_name, now, now),
        )
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row)


def _maybe_migrate_legacy(new_user_id: str) -> dict[str, int] | None:
    """On first Apple account, fold AUTH_LEGACY_USER_ID data into the new id.

    One-shot: only runs when the new user has no rows yet worth keeping and the
    legacy id still has data. Uses usermerge so every user_id table moves.
    """
    legacy = get_settings().auth_legacy_user_id.strip()
    if not legacy or legacy == new_user_id:
        return None
    from . import usermerge

    with connect() as conn:
        # Don't steal data if another Apple user already claimed this legacy id.
        claimed = conn.execute(
            "SELECT id FROM users WHERE id = ? OR legacy_user_id = ?",
            (legacy, legacy),
        ).fetchone()
        if claimed and claimed["id"] != new_user_id:
            return None
        # Skip if legacy has nothing.
        preview = usermerge.merge_user(legacy, new_user_id, dry_run=True, conn=conn)
        if not preview:
            return None
        moved = usermerge.merge_user(legacy, new_user_id, dry_run=False, conn=conn)
        conn.execute(
            "UPDATE users SET legacy_user_id = ?, updated_at = ? WHERE id = ?",
            (legacy, _iso(_utcnow()), new_user_id),
        )
    logger.info("migrated legacy user %s → %s (%s)", legacy, new_user_id, moved)
    return moved


def upsert_apple_user(
    *,
    apple_sub: str,
    email: str | None = None,
    display_name: str | None = None,
) -> dict:
    existing = get_user_by_apple_sub(apple_sub)
    if existing:
        # Refresh email/name when Apple sends them (usually only on first auth).
        updates: list[str] = []
        params: list = []
        if email and not existing.get("email"):
            updates.append("email = ?")
            params.append(email)
        if display_name and not existing.get("display_name"):
            updates.append("display_name = ?")
            params.append(display_name)
        if updates:
            updates.append("updated_at = ?")
            params.append(_iso(_utcnow()))
            params.append(existing["id"])
            with connect() as conn:
                conn.execute(
                    f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
                    params,
                )
            return get_user(existing["id"]) or existing
        return existing

    user_id = new_user_id()
    user = _insert_user(
        user_id=user_id,
        apple_sub=apple_sub,
        email=email,
        display_name=display_name,
    )
    _maybe_migrate_legacy(user_id)
    return get_user(user_id) or user


def create_session(user_id: str, *, ttl_days: int | None = None) -> str:
    """Insert a session; return the plaintext token (shown once)."""
    s = get_settings()
    days = ttl_days if ttl_days is not None else s.auth_session_days
    token = secrets.token_urlsafe(32)
    now = _utcnow()
    expires = now + timedelta(days=max(1, days))
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (_hash_token(token), user_id, _iso(now), _iso(expires)),
        )
    return token


def revoke_session(token: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),)
        )
        return cur.rowcount > 0


def revoke_all_sessions(user_id: str) -> int:
    with connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        return cur.rowcount


def user_id_for_token(token: str) -> str | None:
    if not token:
        return None
    now = _iso(_utcnow())
    with connect() as conn:
        row = conn.execute(
            "SELECT user_id, expires_at FROM sessions WHERE token_hash = ?",
            (_hash_token(token),),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] < now:
            conn.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (_hash_token(token),)
            )
            return None
    return row["user_id"]


def bearer_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    # Also accept X-Session-Token for the minimal web page / curl.
    alt = request.headers.get("X-Session-Token", "").strip()
    return alt or None


def user_from_request(request: Request) -> str | None:
    token = bearer_token(request)
    if not token:
        return None
    return user_id_for_token(token)


def require_user(request: Request) -> str:
    uid = user_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="sign in required")
    from . import llm_budget

    llm_budget.set_user(uid)
    return uid


def email_is_allowed(email: str | None) -> bool:
    """True when the invite allowlist is empty or ``email`` is on it."""
    allowed = get_settings().allowed_emails
    if not allowed:
        return True
    return (email or "").strip().lower() in allowed


def require_apply_access(request: Request) -> None:
    """Gate personal JSON APIs: session, matching apply token, or fail-open.

    Production sets ``AUTH_FAIL_OPEN=false`` so a blank APPLY_API_TOKEN is not
    a hole. Local tests leave it true so ``?user=`` still works without a
    session.
    """
    if user_from_request(request):
        return
    expected = get_settings().apply_api_token.strip()
    got = request.headers.get("X-Apply-Token", "").strip()
    if expected and got == expected:
        return
    if get_settings().auth_fail_open and not expected:
        return
    raise HTTPException(status_code=401, detail="sign in required")


def resolve_user(request: Request, claimed: str | None = None) -> str:
    """Session always wins. Never fall back to dashboard.default_user().

    With a session, query/body ``user`` is ignored (testers cannot pick
    another account). Extension/worker may send X-Apply-Token + claimed user.
    """
    from . import llm_budget

    session_uid = user_from_request(request)
    if session_uid:
        llm_budget.set_user(session_uid)
        return session_uid
    require_apply_access(request)
    uid = (claimed or "").strip()
    if not uid:
        raise HTTPException(status_code=401, detail="sign in required")
    llm_budget.set_user(uid)
    return uid


def html_user(request: Request, claimed: str | None = None) -> str:
    """HTML dashboards: session user in prod; ``?user=`` only when fail-open."""
    from . import dashboard as dash

    session_uid = user_from_request(request)
    if session_uid:
        return session_uid
    if get_settings().auth_fail_open:
        return (claimed or "").strip() or dash.default_user()
    raise HTTPException(status_code=401, detail="sign in required")


def sign_in_with_apple(
    identity_token: str,
    *,
    email: str | None = None,
    display_name: str | None = None,
) -> dict:
    """Verify Apple token → upsert user → mint session. Returns public payload."""
    claims = verify_apple_identity_token(identity_token)
    # Prefer client-supplied name/email (Apple only sends them once) over claims.
    claim_email = claims.get("email")
    existing = get_user_by_apple_sub(claims["sub"])
    check_email = email or claim_email or (existing.get("email") if existing else None)
    if not email_is_allowed(check_email):
        raise HTTPException(
            status_code=403,
            detail="this beta is invite-only",
        )
    user = upsert_apple_user(
        apple_sub=claims["sub"],
        email=email or claim_email,
        display_name=display_name,
    )
    token = create_session(user["id"])
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "email": user.get("email"),
            "display_name": user.get("display_name"),
        },
    }


def sign_in_dev(*, display_name: str | None = None, user_id: str | None = None) -> dict:
    """Mint a session without Apple. Gated by AUTH_ALLOW_DEV_LOGIN."""
    if not get_settings().auth_allow_dev_login:
        raise HTTPException(status_code=403, detail="dev login disabled")
    settings = get_settings()
    uid = (
        (user_id or "").strip()
        or settings.auth_dev_user_id.strip()
        or new_user_id()
    )
    existing = get_user(uid)
    if existing is None:
        _insert_user(
            user_id=uid,
            apple_sub=f"dev:{uid}",
            email=None,
            display_name=display_name or "Dev User",
        )
        _maybe_migrate_legacy(uid)
    token = create_session(uid)
    user = get_user(uid)
    assert user is not None
    return {
        "token": token,
        "user": {
            "id": user["id"],
            "email": user.get("email"),
            "display_name": user.get("display_name"),
        },
    }


def reset_for_tests() -> None:
    """Drop the cached JWKS client between tests."""
    global _jwks_client
    _jwks_client = None
