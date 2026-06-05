"""RSS / Atom job feeds — discover roles without naming a company first.

``board_token`` is a feed id from ``FEEDS`` (e.g. ``hn-hiring``) or a raw feed URL.
"""
from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET

import logging

from .base import JobPosting, strip_html

logger = logging.getLogger("jobsources.rss")

# Curated free feeds. Add ids to JOB_WIDE_RSS_FEEDS in .env.
FEEDS: dict[str, dict[str, str]] = {
    "hn-hiring": {
        "url": "https://hnrss.org/whoishiring",
        "label": "HN Who's Hiring",
    },
    "remoteok": {
        "url": "https://remoteok.com/remote-jobs.rss",
        "label": "Remote OK",
    },
    "weworkremotely": {
        "url": "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "label": "We Work Remotely (Programming)",
    },
}

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
_HN_COMPANY = re.compile(
    r"^([^|\(\-\n]+?)(?:\s*[\|\(\-]\s*|\s+is\s+hiring|\s+hiring\b)",
    re.I,
)


def list_feed_ids() -> list[str]:
    return list(FEEDS.keys())


def resolve_feed(token: str) -> dict | None:
    """Resolve a feed id or URL to a fetchable feed."""
    key = (token or "").strip().lower()
    if key in FEEDS:
        meta = FEEDS[key]
        return {"feed_id": key, "url": meta["url"], "label": meta["label"]}
    if token.startswith("http://") or token.startswith("https://"):
        return {"feed_id": key or "custom", "url": token.strip(), "label": "Custom RSS"}
    return None


def _fetch_xml(url: str) -> bytes | None:
    import httpx

    try:
        resp = httpx.get(
            url,
            timeout=12.0,
            follow_redirects=True,
            headers={"User-Agent": "job-search-tool/1.0", "Accept": "application/rss+xml,*/*"},
        )
        resp.raise_for_status()
        return resp.content
    except Exception:  # noqa: BLE001
        logger.warning("rss fetch failed: %s", url, exc_info=True)
        return None


def _text(el) -> str:
    if el is None:
        return ""
    return (el.text or "").strip() + "".join(
        (t.tail or "").strip() for t in el.iter() if t is not el and t.tail
    ).strip()


def _parse_rss_channel(channel, feed_id: str, label: str) -> list[JobPosting]:
    out: list[JobPosting] = []
    for item in channel.findall("item"):
        title = _text(item.find("title"))
        link = _text(item.find("link"))
        guid = _text(item.find("guid")) or link
        desc = strip_html(_text(item.find("description")))
        pub = _text(item.find("pubDate"))
        if not link and not guid:
            continue
        company = _company_from_item(title, desc, feed_id)
        ext = hashlib.sha256((guid or link).encode()).hexdigest()[:32]
        out.append(
            JobPosting(
                source="rss",
                external_id=f"{feed_id}:{ext}",
                title=title or "Role",
                url=link,
                company=company,
                location=_location_hint(desc, feed_id),
                description=desc,
                posted_at=pub,
            )
        )
    return out


def _parse_atom(root, feed_id: str, label: str) -> list[JobPosting]:
    out: list[JobPosting] = []
    for entry in root.findall("atom:entry", _ATOM_NS):
        title = _text(entry.find("atom:title", _ATOM_NS))
        link_el = entry.find("atom:link[@rel='alternate']", _ATOM_NS) or entry.find(
            "atom:link", _ATOM_NS
        )
        link = link_el.get("href", "") if link_el is not None else ""
        entry_id = _text(entry.find("atom:id", _ATOM_NS)) or link
        summary = strip_html(
            _text(entry.find("atom:summary", _ATOM_NS))
            or _text(entry.find("atom:content", _ATOM_NS))
        )
        updated = _text(entry.find("atom:updated", _ATOM_NS))
        if not entry_id:
            continue
        company = _company_from_item(title, summary, feed_id)
        ext = hashlib.sha256(entry_id.encode()).hexdigest()[:32]
        out.append(
            JobPosting(
                source="rss",
                external_id=f"{feed_id}:{ext}",
                title=title or "Role",
                url=link,
                company=company,
                location=_location_hint(summary, feed_id),
                description=summary,
                posted_at=updated,
            )
        )
    return out


def _company_from_item(title: str, body: str, feed_id: str) -> str:
    if feed_id in ("remoteok", "weworkremotely"):
        # Titles often "Company: Role" on these aggregators.
        if ":" in title:
            return title.split(":", 1)[0].strip()
    m = _HN_COMPANY.match(title.strip())
    if m:
        return m.group(1).strip().title()
    for line in body.splitlines()[:8]:
        line = line.strip()
        if not line or len(line) > 80:
            continue
        m2 = _HN_COMPANY.match(line)
        if m2:
            return m2.group(1).strip().title()
    return FEEDS.get(feed_id, {}).get("label", "Unknown")


def _location_hint(body: str, feed_id: str) -> str:
    if feed_id == "remoteok" or feed_id == "weworkremotely" or re.search(r"\bremote\b", body, re.I):
        return "Remote"
    m = re.search(r"\b(?:location|based in|office)[:\s]+([^\n|]{3,60})", body, re.I)
    return m.group(1).strip() if m else ""


def _parse_xml(data: bytes, feed_id: str, label: str) -> list[JobPosting]:
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return []
    channel = root.find("channel")
    if channel is not None or root.tag == "rss":
        return _parse_rss_channel(channel if channel is not None else root, feed_id, label)
    if root.tag.endswith("feed"):
        return _parse_atom(root, feed_id, label)
    return []


def fetch(board_token: str) -> list[JobPosting]:
    meta = resolve_feed(board_token)
    if meta is None:
        return []
    raw = _fetch_xml(meta["url"])
    if not raw:
        return []
    return _parse_xml(raw, meta["feed_id"], meta["label"])
