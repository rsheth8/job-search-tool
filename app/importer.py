"""Bulk backfill — get an existing job search into the tracker fast.

Two input shapes, one executor:

  * **Brain-dump** — freeform lines, one application/update/note per line
    ("applied stripe swe", "google oa received", "note ramp referred by Sam").
    Each line goes through the same router the SMS path uses, so it tolerates
    typos and casual phrasing. Offline heuristic router handles the common cases
    with no API key.
  * **CSV** — columns ``company, role, status, applied_at, notes`` (all but
    ``company`` optional; header names are case-insensitive).

Unlike the conversational engine, import **never prompts**: a line it can't
resolve is skipped and reported, never turned into a pending question. This
keeps a 50-line paste a single non-interactive operation. Re-running is safe —
applications already tracked are skipped, not duplicated.
"""
from __future__ import annotations

import csv as _csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import store
from .intents import Intent
from .router import get_router, normalize_status

_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d")


@dataclass
class ImportSummary:
    created: int = 0
    updated: int = 0
    noted: int = 0
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return self.created + self.updated + self.noted

    def render(self) -> str:
        parts = [
            f"Imported: {self.created} added, {self.updated} updated, "
            f"{self.noted} note(s)."
        ]
        if self.skipped:
            parts.append(f"Skipped {len(self.skipped)}:")
            for line, reason in self.skipped[:10]:
                snippet = line if len(line) <= 50 else line[:47] + "..."
                parts.append(f"• {snippet} ({reason})")
            if len(self.skipped) > 10:
                parts.append(f"…and {len(self.skipped) - 10} more.")
        return "\n".join(parts)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:  # last resort: full ISO ("2026-05-01T12:00:00+00:00")
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Shared executor — no prompts, dedupe-aware
# ---------------------------------------------------------------------------

def _add_application(
    user_id: str,
    company: str,
    role: str | None,
    *,
    status: str,
    applied_at: datetime | None,
    raw: str,
    summary: ImportSummary,
) -> None:
    """Create an application unless an equivalent one is already tracked."""
    existing = store.find_application(user_id, company, role=role)
    if existing is None and not role:
        existing = store.find_application(user_id, company)
    if existing is not None:
        summary.skipped.append((raw, f"{company} already tracked"))
        return
    store.create_application(
        user_id, company, role, status=status, source="import",
        raw_sms=raw, applied_at=applied_at,
    )
    summary.created += 1


def _apply_parsed(user_id: str, p, raw: str, summary: ImportSummary) -> None:
    company = p.company
    if p.intent == Intent.APPLY:
        if not company:
            summary.skipped.append((raw, "no company found"))
            return
        _add_application(
            user_id, company, p.role, status="Applied",
            applied_at=None, raw=raw, summary=summary,
        )
    elif p.intent == Intent.UPDATE:
        status = normalize_status(p.status) if p.status else None
        if not company or not status:
            summary.skipped.append((raw, "need company + status"))
            return
        app = store.find_application(user_id, company, role=p.role) or \
            store.find_application(user_id, company)
        if app is None:
            _add_application(
                user_id, company, p.role, status=status,
                applied_at=None, raw=raw, summary=summary,
            )
        else:
            store.update_status(user_id, app["id"], status, raw_sms=raw)
            summary.updated += 1
    elif p.intent == Intent.NOTE:
        note = p.message
        if not company or not note:
            summary.skipped.append((raw, "need company + note text"))
            return
        app = store.find_application(user_id, company)
        if app is None:
            summary.skipped.append((raw, f"{company} not tracked yet"))
            return
        store.add_note(user_id, app["id"], note, raw_sms=raw)
        summary.noted += 1
    else:
        summary.skipped.append((raw, "couldn't interpret"))


# ---------------------------------------------------------------------------
# Brain-dump
# ---------------------------------------------------------------------------

def import_braindump(user_id: str, text: str) -> ImportSummary:
    """Import a freeform multi-line dump, one item per non-empty line."""
    summary = ImportSummary()
    router = get_router()
    for raw_line in (text or "").splitlines():
        line = raw_line.strip().lstrip("-•*").strip()
        if not line:
            continue
        actions = router.parse_actions(line) or []
        if not actions:
            summary.skipped.append((line, "couldn't interpret"))
            continue
        for p in actions:
            _apply_parsed(user_id, p, line, summary)
    return summary


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def import_csv(user_id: str, csv_text: str) -> ImportSummary:
    """Import CSV with case-insensitive headers: company, role, status,
    applied_at, notes. Only ``company`` is required."""
    summary = ImportSummary()
    reader = _csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        norm = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        company = norm.get("company")
        if not company:
            raw = ",".join(v for v in row.values() if v)
            summary.skipped.append((raw or "(blank row)", "no company"))
            continue
        role = norm.get("role") or None
        status = normalize_status(norm.get("status")) or "Applied"
        applied_at = _parse_date(norm.get("applied_at") or norm.get("date"))
        raw = f"{company}" + (f" — {role}" if role else "")
        before = summary.created
        _add_application(
            user_id, company, role, status=status,
            applied_at=applied_at, raw=raw, summary=summary,
        )
        # Attach a note if one was provided and the app was actually created.
        note = norm.get("notes") or norm.get("note")
        if note and summary.created > before:
            app = store.find_application(user_id, company, role=role) or \
                store.find_application(user_id, company)
            if app is not None:
                store.add_note(user_id, app["id"], note, raw_sms=raw)
                summary.noted += 1
    return summary
