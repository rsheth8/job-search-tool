"""Lever public postings API (free, no auth).

    GET https://api.lever.co/v0/postings/{token}?mode=json

``token`` is the company slug in jobs.lever.co/<token>. Response is a JSON list.
"""
from __future__ import annotations

from .base import JobPosting, get_json, iso_from_epoch_ms, strip_html

API = "https://api.lever.co/v0/postings/{token}?mode=json"


def _parse(data, board_token: str) -> list[JobPosting]:
    if not isinstance(data, list):
        return []
    out: list[JobPosting] = []
    for j in data:
        if not isinstance(j, dict):
            continue
        ext = j.get("id")
        if not ext:
            continue
        cats = j.get("categories") or {}
        desc = j.get("descriptionPlain") or strip_html(j.get("description"))
        out.append(
            JobPosting(
                source="lever",
                external_id=str(ext),
                title=(j.get("text") or "").strip(),
                url=j.get("hostedUrl") or j.get("applyUrl") or "",
                company=board_token.strip(),
                location=(cats.get("location") or "").strip(),
                description=strip_html(desc) if desc else "",
                posted_at=iso_from_epoch_ms(j.get("createdAt")),
            )
        )
    return out


def fetch(board_token: str) -> list[JobPosting]:
    return _parse(get_json(API.format(token=board_token)), board_token)
