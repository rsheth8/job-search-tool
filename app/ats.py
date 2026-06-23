"""Which applicant-tracking system (ATS) a URL points at — and therefore whether
the headless worker can auto-fill it.

Auto-submit only works on **first-party ATS application forms** (Greenhouse, Lever,
Ashby): one clean page, native fields, no login, no captcha. Aggregator links
(SerpApi / Google-Jobs `…_apply` redirects), RSS description pages, and login- or
captcha-gated sites have no fillable form on the page — those are handed off to the
desktop browser extension, not automated. See `worker/README.md`.

Pure + dependency-light (stdlib only), so it's shared by the server (to label queue
items + gate `/apply/autosubmit`) and the worker (to bail before filling junk).
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# name -> host regex for the ATSes whose links are a directly fillable form.
_ATS_HOSTS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"(^|\.)greenhouse\.io$", re.I)),
    ("lever", re.compile(r"(^|\.)lever\.co$", re.I)),
    ("ashby", re.compile(r"(^|\.)ashbyhq\.com$", re.I)),
]


def ats_of(url: str | None) -> str | None:
    """The ATS name if ``url`` is a first-party, auto-fillable application form,
    else None (aggregator / RSS / unknown / login-gated)."""
    try:
        host = (urlparse((url or "").strip()).hostname or "").lower()
    except Exception:  # noqa: BLE001 — a malformed URL is simply not fillable
        return None
    if not host:
        return None
    for name, rx in _ATS_HOSTS:
        if rx.search(host):
            return name
    return None


def is_fillable_form(url: str | None) -> bool:
    """True if the headless worker should auto-fill this URL (a first-party ATS
    form). False means hand it off to the desktop extension instead."""
    return ats_of(url) is not None
