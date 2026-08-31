"""Is Claude actually working? One place that can answer.

Every paid call site fails open to a heuristic on purpose -- a dead API must
never cost someone their turn. The cost of that is silence: a wrong key, a
typo'd model name or an exhausted account degrades the whole app to heuristics
and *nothing says so*. ``/health`` reported the configured model name, which is
true whether or not a single call has ever succeeded.

So this module owns three things:

``client()``
    The shared Anthropic client. It wraps ``messages.create`` to record the
    outcome of every call, which is why all ten call sites go through it --
    observability arrives without each of them remembering to report.

``model_looks_valid``
    A cheap shape check on the model id, with no API call. ``ANTHROPIC_MODEL``
    is read straight from the environment, so a bare "sonnet" or "haiku" (not
    real model ids) silently broke every feature. Config treats an implausible
    id as "no LLM configured", so the app skips the paid path instead of paying
    latency for a call that cannot succeed.

``probe()``
    One deliberately tiny real call, on request only, so the operator can prove
    the key works before handing builds to testers.
"""
from __future__ import annotations

import logging
import re
import threading

from .config import get_settings

logger = logging.getLogger("llm_health")

#: Anthropic model ids are "claude-<family>-<version>" with an optional date or
#: "-latest" suffix. Deliberately permissive about the tail -- new models ship
#: often and this must not reject a valid future id -- but it does require the
#: "claude-" prefix and a known family, which is what catches the real mistake
#: (passing an alias like "sonnet" or a name from another vendor).
_MODEL_RE = re.compile(r"^claude-[a-z0-9][a-z0-9.\-]*$")
_FAMILIES = ("haiku", "sonnet", "opus")


def model_looks_valid(model: str | None = None) -> bool:
    """True when ``model`` is shaped like a real Anthropic model id.

    Checked case-sensitively and without normalising, because the value is sent
    to the API verbatim -- "Claude-Haiku-4-5" is not a real id and would 404.
    Config strips surrounding whitespace at load, so that much is already safe.
    """
    name = model if model is not None else get_settings().anthropic_model
    name = (name or "").strip()
    if not _MODEL_RE.match(name):
        return False
    return any(f in name for f in _FAMILIES)


def config_problem() -> str | None:
    """Why paid calls can't run, in one human sentence. None when fine."""
    s = get_settings()
    if not s.anthropic_api_key.strip():
        return "ANTHROPIC_API_KEY is not set — running on heuristics."
    model = (s.anthropic_model or "").strip()
    if not model:
        return "ANTHROPIC_MODEL is empty — running on heuristics."
    if not model_looks_valid(model):
        return (
            f"ANTHROPIC_MODEL={model!r} is not a valid Anthropic model id "
            "(expected e.g. 'claude-haiku-4-5') — running on heuristics."
        )
    return None


def warn_if_misconfigured() -> str | None:
    """Log the config problem once at startup. Returns it for the caller."""
    problem = config_problem()
    if problem and get_settings().anthropic_api_key.strip():
        # A missing key is a normal, supported mode; a *broken* key or model is
        # a mistake someone wants to hear about before testers do.
        logger.error("Claude disabled: %s", problem)
    elif problem:
        logger.info("Claude not configured: %s", problem)
    return problem


# ---------------------------------------------------------------------------
# Call outcome tracking
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_stats: dict = {"ok": 0, "failed": 0, "last_error": None, "last_error_model": None}


def record(*, ok: bool, model: str | None = None, error: BaseException | None = None) -> None:
    with _lock:
        if ok:
            _stats["ok"] += 1
        else:
            _stats["failed"] += 1
            # Type name + trimmed message: enough to tell "bad key" from
            # "bad model" from "rate limited", without leaking a full payload.
            _stats["last_error"] = f"{type(error).__name__}: {str(error)[:200]}" if error else "unknown"
            _stats["last_error_model"] = model


def snapshot() -> dict:
    """Counters for ``/health``. Process-local: resets on deploy, and each Fly
    machine reports its own."""
    with _lock:
        return dict(_stats)


def reset_for_tests() -> None:
    with _lock:
        _stats.update({"ok": 0, "failed": 0, "last_error": None,
                       "last_error_model": None})


# ---------------------------------------------------------------------------
# The shared client
# ---------------------------------------------------------------------------

class _RecordingMessages:
    def __init__(self, inner):
        self._inner = inner

    def create(self, **kw):
        try:
            resp = self._inner.create(**kw)
        except BaseException as exc:  # noqa: BLE001 — record, then re-raise
            record(ok=False, model=kw.get("model"), error=exc)
            raise
        record(ok=True, model=kw.get("model"))
        return resp

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _RecordingClient:
    """Transparent proxy that only intervenes on ``messages.create``."""

    def __init__(self, inner):
        self._inner = inner
        self._messages = _RecordingMessages(inner.messages)

    @property
    def messages(self):
        return self._messages

    def __getattr__(self, name):
        return getattr(self._inner, name)


def client(api_key: str | None = None):
    """Anthropic client whose call outcomes land in ``snapshot()``."""
    import anthropic

    key = api_key if api_key is not None else get_settings().anthropic_api_key
    return _RecordingClient(anthropic.Anthropic(api_key=key))


# ---------------------------------------------------------------------------
# Explicit self-test
# ---------------------------------------------------------------------------

def probe() -> dict:
    """Make one minimal real call and report what happened.

    Costs a single request of a couple of tokens, only when asked. This is the
    check that "valid ANTHROPIC_API_KEY" in the launch checklist actually needs
    -- shape validation can't tell a revoked key from a good one.
    """
    problem = config_problem()
    if problem:
        return {"ok": False, "reason": "config", "detail": problem}
    s = get_settings()
    try:
        resp = client().messages.create(
            model=s.anthropic_model,
            max_tokens=4,
            messages=[{"role": "user", "content": "Reply with: ok"}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
        return {"ok": True, "model": s.anthropic_model, "reply": text[:40]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm probe failed", exc_info=True)
        return {
            "ok": False,
            "reason": "call_failed",
            "model": s.anthropic_model,
            "detail": f"{type(exc).__name__}: {str(exc)[:300]}",
        }
