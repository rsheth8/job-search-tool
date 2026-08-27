"""Which applicant-tracking system (ATS) a URL points at.

Known first-party hosts (Greenhouse, Lever, Ashby) are a *confidence boost* for
autofill — not a hard gate. The headless worker may attempt any http(s) apply URL;
``formprobe`` decides after navigation whether the page is fillable, a login wall,
or a captcha that needs the human.

Pure + dependency-light (stdlib only), shared by the server and the worker.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# name -> host regex for ATSes we know well (high-confidence autofill).
_ATS_HOSTS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"(^|\.)greenhouse\.io$", re.I)),
    ("lever", re.compile(r"(^|\.)lever\.co$", re.I)),
    ("ashby", re.compile(r"(^|\.)ashbyhq\.com$", re.I)),
]


def ats_of(url: str | None) -> str | None:
    """The ATS name if ``url`` is a known first-party board host, else None."""
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
    """True if this is a *known* first-party ATS host (UI confidence label).

    Autopilot itself uses ``may_autosubmit`` + live ``formprobe`` — do not use this
    to refuse a fill request.
    """
    return ats_of(url) is not None


def may_autosubmit(url: str | None) -> bool:
    """True if the worker should be allowed to *attempt* this URL.

    Any absolute http(s) URL qualifies; page content decides success. Empty or
    non-http schemes (mailto, javascript) are refused.
    """
    raw = (url or "").strip()
    if not raw:
        return False
    try:
        parsed = urlparse(raw)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return bool(parsed.netloc)
