"""Labels Fill skipped become the phrasing table.

ATS question wording is a long tail. Rather than import a giant synonym list,
we record unmatched / unfilled labels from real fills and grow FIELD_RULES from
jobs the user actually applies to.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from .db import connect

_REASONS = frozenset({"unmatched", "empty", "no_option", "essay"})
_MAX_PER_REQUEST = 40
_LABEL_MAX = 160
_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"^[\s:;,.!?*-]+|[\s:;,.!?*-]+$")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_label(label: str) -> str:
    t = _WS.sub(" ", (label or "").strip().lower())
    t = _PUNCT.sub("", t)
    return t[:_LABEL_MAX]


def record_skips(
    user_id: str,
    skips: list,
    *,
    url: str = "",
    posting_id: int | None = None,
) -> dict:
    """Upsert skipped labels for ``user_id``. Returns how many were stored."""
    uid = (user_id or "").strip()
    if not uid:
        return {"stored": 0, "skipped": 0}
    rows = _clean(skips)
    if not rows:
        return {"stored": 0, "skipped": 0}
    now = _now()
    url = (url or "").strip()[:500]
    stored = 0
    with connect() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO fill_skips (
                    user_id, label, label_norm, reason, key, url, posting_id,
                    count, first_seen, last_seen, options
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(user_id, label_norm, reason) DO UPDATE SET
                    count = count + 1,
                    last_seen = excluded.last_seen,
                    url = COALESCE(NULLIF(excluded.url, ''), url),
                    posting_id = COALESCE(excluded.posting_id, posting_id),
                    key = COALESCE(excluded.key, key),
                    options = COALESCE(excluded.options, options),
                    label = excluded.label
                """,
                (
                    uid, row["label"], row["norm"], row["reason"], row["key"],
                    url, posting_id, now, now, row["options"],
                ),
            )
            stored += 1
    return {"stored": stored, "skipped": max(0, len(skips or []) - stored)}


def list_skips(user_id: str, limit: int = 50) -> list[dict]:
    uid = (user_id or "").strip()
    if not uid:
        return []
    limit = max(1, min(int(limit or 50), 200))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT label, reason, key, url, posting_id, count, first_seen,
                   last_seen, options
              FROM fill_skips
             WHERE user_id = ?
             ORDER BY count DESC, last_seen DESC
             LIMIT ?
            """,
            (uid, limit),
        ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        raw = item.get("options")
        if raw:
            try:
                item["options"] = json.loads(raw)
            except json.JSONDecodeError:
                item["options"] = None
        else:
            item["options"] = None
        out.append(item)
    return out


def _clean(skips: list) -> list[dict]:
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for raw in skips or []:
        if len(out) >= _MAX_PER_REQUEST:
            break
        if not isinstance(raw, dict):
            continue
        reason = str(raw.get("reason") or "").strip().lower()
        if reason not in _REASONS:
            continue
        label = _WS.sub(" ", str(raw.get("label") or "").strip())[:_LABEL_MAX]
        if len(label) < 4:
            continue
        norm = normalize_label(label)
        if len(norm) < 4:
            continue
        sig = (norm, reason)
        if sig in seen:
            continue
        seen.add(sig)
        key = str(raw.get("key") or "").strip() or None
        opts = raw.get("options") or raw.get("detail")
        options = None
        if isinstance(opts, list):
            options = json.dumps([str(x)[:80] for x in opts[:12]])
        elif isinstance(opts, str) and opts.strip():
            options = json.dumps([opts.strip()[:200]])
        out.append({
            "label": label,
            "norm": norm,
            "reason": reason,
            "key": key,
            "options": options,
        })
    return out
