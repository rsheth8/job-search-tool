"""Public Workday career sites — the same JSON the company's careers page loads.

There is no Workday-for-developers jobs API. Each employer publishes a careers
site on ``*.myworkdayjobs.com``; that page's own Candidate Experience (CXS)
feed is unauthenticated JSON, the same class of thing as Greenhouse's public
board API.

Legal / operational rules (do not "fix" these):

* Identify as ``JobPilot/1.0``. Never spoof a browser UA or retry past 403/429.
* Need the company's careers URL (host + site). There is no public tenant map.
* Rotate a small curated list per tick — Workday sits behind bot protection.

``board_token`` is ``{host}/{site}`` or a full careers URL.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from ..config import get_settings
from .base import JobPosting, post_json, strip_html

logger = logging.getLogger("jobsources.workday")

_LOCALE = re.compile(r"^en(?:-[A-Za-z]{2})?$", re.I)
_PAGE = 20  # Workday silently returns [] above this.


@dataclass(frozen=True)
class Board:
    host: str
    tenant: str
    site: str
    name: str = ""

    @property
    def token(self) -> str:
        return f"{self.host}/{self.site}"

    @property
    def careers_url(self) -> str:
        return f"https://{self.host}/en-US/{self.site}"

    @property
    def jobs_api(self) -> str:
        return f"https://{self.host}/wday/cxs/{self.tenant}/{self.site}/jobs"

    def posting_url(self, external_path: str) -> str:
        path = (external_path or "").strip()
        if not path.startswith("/"):
            path = "/" + path
        return f"https://{self.host}/en-US/{self.site}{path}"


def parse_board(raw: str, *, name: str = "") -> Board | None:
    """Careers URL or ``host/site`` token → Board. None if it isn't Workday."""
    text = (raw or "").strip()
    if not text:
        return None
    if "://" not in text and "/" in text and not text.startswith("/"):
        text = "https://" + text
    if "://" not in text:
        return None
    try:
        parsed = urlparse(text)
    except Exception:  # noqa: BLE001
        return None
    host = (parsed.hostname or "").lower()
    if not host.endswith("myworkdayjobs.com"):
        return None
    labels = [p for p in host.split(".") if p]
    if len(labels) < 3:
        return None
    tenant = labels[0]
    parts = [p for p in (parsed.path or "").split("/") if p]
    site = ""
    for p in parts:
        if _LOCALE.match(p) or p.lower() == "job":
            if p.lower() == "job":
                break
            continue
        site = p
        break
    if not site:
        return None
    return Board(host=host, tenant=tenant, site=site, name=(name or "").strip())


def _title_from_path(external_path: str) -> str:
    slug = (external_path or "").rstrip("/").split("/")[-1]
    slug = re.sub(r"_[A-Z]{1,5}-?\d.*$", "", slug)
    slug = re.sub(r"[_-]+", " ", slug).strip()
    return slug


def _job_id(row: dict, external_path: str) -> str:
    bullets = row.get("bulletFields") or []
    if isinstance(bullets, list):
        for b in bullets:
            text = str(b or "").strip()
            if text:
                return text
    slug = (external_path or "").rstrip("/").split("/")[-1]
    return slug or ""


def _parse(data, board: Board) -> list[JobPosting]:
    if not isinstance(data, dict):
        return []
    out: list[JobPosting] = []
    for row in data.get("jobPostings") or []:
        if not isinstance(row, dict):
            continue
        path = (row.get("externalPath") or "").strip()
        ext = _job_id(row, path)
        if not ext:
            continue
        title = (row.get("title") or "").strip() or _title_from_path(path)
        loc = (row.get("locationsText") or row.get("location") or "").strip()
        posted = (row.get("postedOn") or row.get("postedDate") or "").strip()
        out.append(
            JobPosting(
                source="workday",
                external_id=ext,
                title=title,
                url=board.posting_url(path) if path else board.careers_url,
                company=board.name or board.tenant.replace("-", " ").title(),
                location=loc,
                description=strip_html(title + (". " + loc if loc else "")),
                posted_at=posted,
            )
        )
    return out


def fetch(board_token: str) -> list[JobPosting]:
    board = parse_board(board_token)
    if board is None:
        return []
    settings = get_settings()
    cap = max(1, int(settings.job_workday_max_jobs_per_board or 25))
    out: list[JobPosting] = []
    offset = 0
    while len(out) < cap:
        payload = {
            "appliedFacets": {},
            "limit": _PAGE,
            "offset": offset,
            "searchText": "",
        }
        data = post_json(
            board.jobs_api,
            payload,
            extra_headers={"Referer": board.careers_url},
        )
        if data is None:
            break
        page = _parse(data, board)
        if not page:
            break
        out.extend(page)
        offset += _PAGE
        total = data.get("total")
        if isinstance(total, int) and offset >= total:
            break
        if len(page) < _PAGE:
            break
    return out[:cap]


def parse_job_url(url: str) -> JobPosting | None:
    """Build a posting from a Workday job URL without hitting the network."""
    board = parse_board(url)
    if board is None:
        return None
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return None
    parts = [p for p in (parsed.path or "").split("/") if p]
    if "job" not in [p.lower() for p in parts]:
        return None
    idx = next(i for i, p in enumerate(parts) if p.lower() == "job")
    path = "/" + "/".join(parts[idx:])
    ext = _job_id({}, path)
    if not ext:
        return None
    known = lookup_token(board.token)
    name = (known["name"] if known else "") or board.tenant.replace("-", " ").title()
    title = _title_from_path(path) or "Workday job"
    return JobPosting(
        source="workday",
        external_id=ext,
        title=title,
        url=board.posting_url(path),
        company=name,
        description=title,
    )


# ---------------------------------------------------------------------------
# Curated directory
# ---------------------------------------------------------------------------

def _data_path() -> Path:
    path = Path(get_settings().job_workday_data_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return path


@lru_cache(maxsize=1)
def load_boards() -> list[dict]:
    try:
        data = json.loads(_data_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("workday directory missing or invalid")
        return []
    rows = data.get("boards") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        board = parse_board(str(raw.get("url") or ""), name=str(raw.get("name") or ""))
        if board is None or board.token.lower() in seen:
            continue
        seen.add(board.token.lower())
        secs = {str(s).strip().lower() for s in (raw.get("sectors") or []) if s}
        out.append({
            "name": board.name or board.tenant.title(),
            "token": board.token,
            "url": board.careers_url,
            "sectors": secs or {"software"},
        })
    return out


def reset_cache() -> None:
    load_boards.cache_clear()


def lookup_company(name: str) -> dict | None:
    """Curated Workday board for a company name, if we have the careers URL."""
    from .. import catalog

    want = catalog._norm_name(name)
    if not want:
        return None
    for row in load_boards():
        if catalog._norm_name(row["name"]) == want:
            return row
        host = row["token"].split("/", 1)[0]
        tenant = host.split(".")[0]
        if catalog._norm_name(tenant) == want:
            return row
    return None


def lookup_token(token: str) -> dict | None:
    key = (token or "").strip().lower()
    for row in load_boards():
        if row["token"].lower() == key:
            return row
    return None


def _flat(sectors: frozenset[str] | None) -> list[dict]:
    rows = load_boards()
    if not sectors:
        return rows
    return [r for r in rows if r["sectors"] & set(sectors)]


def fetch_directory_batch(
    *,
    user_id: str = "",
    sectors: frozenset[str] | None = None,
) -> list[JobPosting]:
    """Rotate the next slice of curated Workday careers sites."""
    from .. import jobstore

    settings = get_settings()
    pairs = _flat(sectors)
    n = max(0, int(settings.job_workday_boards_per_tick or 0))
    if not pairs or n <= 0:
        return []
    sec = ",".join(sorted(sectors or [])) or "*"
    cursor_key = f"workday:{user_id or 'global'}:{sec}"
    start = jobstore.get_directory_cursor(cursor_key) % len(pairs)
    selected = [pairs[(start + i) % len(pairs)] for i in range(min(n, len(pairs)))]
    jobstore.set_directory_cursor(start + n, cursor_key)

    out: list[JobPosting] = []
    for row in selected:
        try:
            posts = fetch(row["token"])
        except Exception:  # noqa: BLE001
            logger.warning("workday probe failed %s", row["token"], exc_info=True)
            continue
        for p in posts:
            p.company = p.company or row["name"]
            p.external_id = f"workday:{row['token']}:{p.external_id}"
            out.append(p)
    return out


def board_count(sectors: frozenset[str] | None = None) -> int:
    return len(_flat(sectors))
