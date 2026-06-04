"""Generic RSS / Atom job feed (free, no auth).

``board_token`` is the feed URL. Covers RSS 2.0 (``<item>``) and Atom
(``<entry>``) — enough for "Who is hiring" aggregations and company job feeds.
Like the ATS adapters, any error returns ``[]`` (logged) rather than raising, so
one broken feed never breaks a discovery tick.

Unlike Greenhouse/Lever/Ashby, RSS uses a full URL token, so it does NOT take
part in ``resolve_board`` slug auto-detection — a feed is tracked explicitly.
"""
from __future__ import annotations

import logging
from xml.etree import ElementTree as ET

from .base import HTTP_TIMEOUT_SECONDS, JobPosting, strip_html

logger = logging.getLogger("jobsources")

_ATOM = "{http://www.w3.org/2005/Atom}"


def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def _parse(xml_text: str, board_token: str) -> list[JobPosting]:
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.warning("rss parse failed for %s", board_token, exc_info=True)
        return []

    out: list[JobPosting] = []
    # RSS 2.0 — channel/item.
    for item in root.iter("item"):
        title = _text(item.find("title"))
        link = _text(item.find("link"))
        guid = _text(item.find("guid")) or link or title
        if not (title or link):
            continue
        out.append(
            JobPosting(
                source="rss",
                external_id=guid,
                title=title,
                url=link,
                description=strip_html(_text(item.find("description"))),
                posted_at=_text(item.find("pubDate")),
            )
        )
    if out:
        return out

    # Atom — entry; link is an attribute, not text.
    for entry in root.iter(f"{_ATOM}entry"):
        title = _text(entry.find(f"{_ATOM}title"))
        link_el = entry.find(f"{_ATOM}link")
        link = link_el.get("href", "") if link_el is not None else ""
        eid = _text(entry.find(f"{_ATOM}id")) or link or title
        summary = _text(entry.find(f"{_ATOM}summary")) or _text(
            entry.find(f"{_ATOM}content")
        )
        if not (title or link):
            continue
        out.append(
            JobPosting(
                source="rss",
                external_id=eid,
                title=title,
                url=link,
                description=strip_html(summary),
                posted_at=_text(entry.find(f"{_ATOM}updated")),
            )
        )
    return out


def fetch(board_token: str) -> list[JobPosting]:
    """Fetch and parse one feed URL. Any network/parse error -> []."""
    import httpx  # lazy: offline/test paths never import it

    try:
        resp = httpx.get(
            board_token,
            timeout=HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": "job-search-tool/1.0"},
        )
        resp.raise_for_status()
        return _parse(resp.text, board_token)
    except Exception:  # noqa: BLE001 — degrade to [] on any error
        logger.warning("rss fetch failed: %s", board_token, exc_info=True)
        return []
