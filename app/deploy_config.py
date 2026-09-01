"""Does the running app agree with the fly.toml it shipped with?

Fly injects ``[env]`` from fly.toml and ``fly secrets`` into the same process
environment, and **secrets win**. So a secret set once, years ago, silently
overrides every later edit to fly.toml — the deploy goes green, the file says
one thing, and the app does another.

That is not hypothetical. ``JOB_SOURCES_ENABLED`` was a secret holding a stale
list, so a release that added four job sources to fly.toml shipped them
switched off: the code was there, the boards file was there, the feature was
inert, and nothing anywhere said so.

fly.toml is baked into the image (``COPY . .``), so the process can read what
it was *supposed* to be configured with and compare. Any key whose live value
differs from the file is being shadowed by something.

Key **names** only, never values: /health is public and the shadowing value is
usually a secret.
"""
from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path


def _fly_toml() -> Path:
    return Path(__file__).resolve().parents[1] / "fly.toml"


@lru_cache(maxsize=1)
def declared_env() -> dict[str, str]:
    """The ``[env]`` table from fly.toml, as strings. Empty if unreadable."""
    try:
        data = tomllib.loads(_fly_toml().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    env = data.get("env")
    if not isinstance(env, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in env.items():
        if isinstance(value, bool):
            out[str(key)] = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            out[str(key)] = str(value)
    return out


def shadowed_keys(environ: dict[str, str] | None = None) -> list[str]:
    """fly.toml ``[env]`` keys whose live value isn't what the file says.

    Returns names, sorted. Empty off Fly: a developer's shell disagreeing with
    fly.toml is normal and says nothing, so this would be pure noise locally.
    """
    env = os.environ if environ is None else environ
    if not env.get("FLY_APP_NAME"):
        return []
    out = [
        key for key, declared in declared_env().items()
        if key in env and env[key].strip() != declared.strip()
    ]
    return sorted(out)


def reset_cache() -> None:
    declared_env.cache_clear()
