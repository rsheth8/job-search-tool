"""Safe error payloads, request IDs, and a DB ping for testers.

Unhandled exceptions never leak a traceback to the client. Every response
carries ``X-Request-Id`` so a tester's "it broke" can be matched to a log line.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("errors")

INTERNAL_DETAIL = (
    "Something went wrong on our side. Try again — if it keeps happening, "
    "send feedback from Settings."
)
INVALID_DETAIL = "That request wasn't valid. Try again."


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def request_id(request: Request) -> str:
    rid = getattr(request.state, "request_id", None)
    if isinstance(rid, str) and rid.strip():
        return rid.strip()
    incoming = (request.headers.get("X-Request-Id") or "").strip()
    if incoming:
        return incoming[:64]
    return new_request_id()


def payload(detail: str, *, code: str, request_id: str) -> dict[str, str]:
    return {"detail": detail, "code": code, "request_id": request_id}


def ping_db() -> bool:
    """True when SQLite answers ``SELECT 1``. Failures stay in the log."""
    try:
        from .db import connect

        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
        return True
    except Exception:  # noqa: BLE001 — health must never raise
        logger.exception("db ping failed")
        return False


def _capture(exc: BaseException, rid: str) -> None:
    try:
        import sentry_sdk

        sentry_sdk.set_tag("request_id", rid)
        sentry_sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001 — reporting must not mask the original
        pass


class RequestIdMiddleware:
    """Pure ASGI — ``BaseHTTPMiddleware`` re-raises handled 500s to testers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = new_request_id()
        for key, val in scope.get("headers") or []:
            if key == b"x-request-id":
                raw = val.decode("ascii", "ignore").strip()
                if raw:
                    rid = raw[:64]
                break
        scope.setdefault("state", {})
        scope["state"]["request_id"] = rid

        async def send_with_id(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-Id"] = rid
                status = int(message.get("status") or 0)
                path = scope.get("path") or ""
                method = scope.get("method") or ""
                if status >= 500:
                    ua = ""
                    for k, v in scope.get("headers") or []:
                        if k == b"user-agent":
                            ua = v.decode("ascii", "ignore")[:80]
                            break
                    logger.error(
                        "%s %s -> %s rid=%s ua=%s", method, path, status, rid, ua
                    )
                elif status >= 400 and path != "/health":
                    logger.info("%s %s -> %s rid=%s", method, path, status, rid)
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception as exc:
            # Starlette parks ``Exception`` handlers on ServerErrorMiddleware and
            # re-raises after sending — TestClient then explodes. Swallow here so
            # testers (and tests) always get JSON, never a traceback.
            if isinstance(exc, StarletteHTTPException):
                raise
            path = scope.get("path") or ""
            method = scope.get("method") or ""
            logger.exception("unhandled %s %s rid=%s", method, path, rid)
            _capture(exc, rid)
            response = JSONResponse(
                status_code=500,
                content=payload(
                    INTERNAL_DETAIL, code="internal_error", request_id=rid
                ),
                headers={"X-Request-Id": rid},
            )
            await response(scope, receive, send_with_id)


def install(app: FastAPI) -> None:
    """Wire request-id middleware + handlers."""
    app.add_middleware(RequestIdMiddleware)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        rid = request_id(request)
        return JSONResponse(
            status_code=422,
            content=payload(INVALID_DETAIL, code="invalid_request", request_id=rid),
            headers={"X-Request-Id": rid},
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(request: Request, exc: StarletteHTTPException):
        rid = request_id(request)
        detail = exc.detail
        if not isinstance(detail, str) or not detail.strip():
            detail = (
                "Something went wrong."
                if exc.status_code >= 500
                else "Request failed."
            )
        code = "http_error"
        if exc.status_code == 401:
            code = "auth_required"
        elif exc.status_code == 403:
            code = "forbidden"
        elif exc.status_code == 404:
            code = "not_found"
        elif exc.status_code >= 500:
            code = "internal_error"
        body: dict[str, Any] = payload(detail, code=code, request_id=rid)
        return JSONResponse(
            status_code=exc.status_code,
            content=body,
            headers={"X-Request-Id": rid},
        )
