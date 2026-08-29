"""Which applicant-tracking system (ATS) a URL points at.

Known first-party hosts (Greenhouse, Lever, Ashby) are a *confidence boost* for
autofill in the iOS WebView — not a hard gate. ``formprobe`` decides after
navigation whether the page is fillable, a login wall, or needs the human.

Workable / SmartRecruiters are recognized for discovery (directory learning +
``ats_of``). iOS Fill still runs on those pages — ``apply_kind`` is a
confidence label, not a hard gate. ``formprobe`` decides after navigation.

Pure + dependency-light (stdlib only), shared by the server and clients.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

# name -> host regex for ATSes we know (discovery + labeling).
_ATS_HOSTS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"(^|\.)greenhouse\.io$", re.I)),
    ("lever", re.compile(r"(^|\.)lever\.co$", re.I)),
    ("ashby", re.compile(r"(^|\.)ashbyhq\.com$", re.I)),
    ("workable", re.compile(r"(^|\.)workable\.com$", re.I)),
    ("smartrecruiters", re.compile(r"(^|\.)smartrecruiters\.com$", re.I)),
]

# Ranking / labels: these hosts are the high-confidence Autofill set.
# Fill still runs on other public HTML; formprobe is the gate.
FILLABLE_SOURCES = frozenset({"greenhouse", "lever", "ashby"})
# Company ATS we can open directly even when iOS can't fill the form.
DIRECT_SOURCES = FILLABLE_SOURCES | {"workable", "smartrecruiters"}

# Board tokens that are Simplify proxies / ATS plumbing, not a company board.
_SKIP_TOKENS = frozenset({
    "", "www", "www2", "api", "jobs", "job", "apply", "j", "embed",
    "boards", "job-boards", "internshiplist2000", "simplify", "oneclick",
    "careers", "app", "cdn", "static",
})
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
# Sources whose board tokens are case-sensitive (SmartRecruiters identifiers).
CASE_SENSITIVE_SOURCES = frozenset({"smartrecruiters"})


def ats_of(url: str | None) -> str | None:
    """The ATS name if ``url`` is a known board host, else None."""
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
    """True if this is a *high-confidence Autofill* first-party ATS host.

    Not a hard gate — iOS Fill still runs on other public HTML; formprobe decides.
    """
    return ats_of(url) in FILLABLE_SOURCES


def apply_kind(url: str | None, source: str | None = None) -> str:
    """How the phone should label this apply link.

    ``autofill`` — Greenhouse / Lever / Ashby; the in-app engine is most reliable.
    ``direct`` — company ATS (Workable / SmartRecruiters); Fill still runs.
    ``browser`` — unknown / RSS; open in-app and Fill if a public form appears.
    """
    if is_fillable_form(url):
        return "autofill"
    src = (source or "").strip().lower()
    if ats_of(url) in DIRECT_SOURCES or src in DIRECT_SOURCES:
        return "direct"
    return "browser"


def normalize_board_token(source: str, token: str) -> str:
    """Canonical board token for directory storage / probing."""
    raw = (token or "").strip()
    if (source or "").lower() in CASE_SENSITIVE_SOURCES:
        return raw
    return raw.lower()


def board_from_url(url: str | None) -> tuple[str, str] | None:
    """``(source, board_token)`` if ``url`` is a company ATS board, else None.

    Used to grow the rotating directory from swelist / RSS / YC apply links.
    """
    raw = (url or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except Exception:  # noqa: BLE001
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    path = (parsed.path or "").strip("/")
    parts = [p for p in path.split("/") if p]
    qs = parse_qs(parsed.query)

    source: str | None = None
    token = ""

    if host.endswith("greenhouse.io"):
        source = "greenhouse"
        if host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
            token = parts[0] if parts else ""
            if token.lower() == "embed":
                token = (qs.get("for") or [""])[0]
        elif host.endswith(".greenhouse.io"):
            sub = host.split(".")[0]
            token = "" if sub in ("boards", "job-boards", "www") else sub
    elif host.endswith("lever.co"):
        source = "lever"
        token = parts[0] if host in ("jobs.lever.co", "jobs.eu.lever.co") and parts else ""
    elif host.endswith("ashbyhq.com"):
        source = "ashby"
        token = parts[0] if host == "jobs.ashbyhq.com" and parts else ""
    elif host.endswith("workable.com"):
        source = "workable"
        if host == "apply.workable.com" and parts:
            token = parts[0]
    elif host.endswith("smartrecruiters.com"):
        source = "smartrecruiters"
        if "jobs.smartrecruiters.com" in host and parts:
            token = parts[0]

    if source is None:
        return None
    token = normalize_board_token(source, token)
    if not token or token.lower() in _SKIP_TOKENS or not _TOKEN_RE.match(token):
        return None
    return (source, token)


_APPLY_SUFFIXES = frozenset({
    "apply", "application", "job_app", "embed", "jobs", "job", "j",
})
_ATS_HOST_ROOT = {
    "greenhouse": "greenhouse.io",
    "lever": "lever.co",
    "ashby": "ashbyhq.com",
    "workable": "workable.com",
    "smartrecruiters": "smartrecruiters.com",
}


def posting_ref(url: str | None) -> tuple[str, str, str] | None:
    """``(source, board_token, job_id)`` for a specific posting, else None.

    Board token alone (the careers index) is not enough — liveness is per req.
    """
    board = board_from_url(url)
    if board is None:
        return None
    source, token = board
    try:
        parsed = urlparse((url or "").strip())
    except Exception:  # noqa: BLE001
        return None
    parts = [p for p in (parsed.path or "").strip("/").split("/") if p]
    qs = parse_qs(parsed.query)
    job_id = ""

    if source == "greenhouse":
        job_id = (qs.get("gh_jid") or qs.get("token") or [""])[0]
        if "jobs" in [p.lower() for p in parts]:
            for i, p in enumerate(parts):
                if p.lower() == "jobs" and i + 1 < len(parts) and parts[i + 1].isdigit():
                    job_id = parts[i + 1]
                    break
        elif not job_id:
            for p in reversed(parts):
                if p.isdigit():
                    job_id = p
                    break
    elif source == "lever":
        # jobs.lever.co/{company}/{id}/apply
        if len(parts) >= 2 and parts[1].lower() not in _APPLY_SUFFIXES:
            job_id = parts[1]
    elif source == "ashby":
        if len(parts) >= 2 and parts[1].lower() not in _APPLY_SUFFIXES:
            job_id = parts[1]
    elif source == "workable":
        for i, p in enumerate(parts):
            if p.lower() == "j" and i + 1 < len(parts):
                job_id = parts[i + 1]
                break
    elif source == "smartrecruiters":
        if len(parts) >= 2 and parts[1].lower() not in _APPLY_SUFFIXES:
            job_id = parts[1]

    job_id = (job_id or "").strip()
    if not job_id or job_id.lower() in _SKIP_TOKENS:
        return None
    return (source, token, job_id)


def json_probe_url(url: str | None) -> str | None:
    """Public JSON endpoint that 404s when this posting is gone, if one exists."""
    ref = posting_ref(url)
    if ref is None:
        return None
    source, token, job_id = ref
    if source == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}"
    if source == "lever":
        return f"https://api.lever.co/v0/postings/{token}/{job_id}"
    if source == "smartrecruiters":
        return (
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{job_id}"
        )
    return None


def same_ats_host(url: str | None, source: str | None) -> bool:
    """True when ``url`` is still on that ATS (not a login wall or aggregator)."""
    root = _ATS_HOST_ROOT.get((source or "").lower())
    if not root:
        return False
    try:
        host = (urlparse((url or "").strip()).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return bool(host) and (host == root or host.endswith("." + root))
