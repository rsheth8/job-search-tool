"""SQLite access layer.

Plain sqlite3 (no ORM) keeps the personal-use MVP dependency-light and fast.
Connections are short-lived and created per call; SQLite handles this well for
a single-user workload.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             TEXT NOT NULL,
    company             TEXT NOT NULL,
    role                TEXT,
    status              TEXT NOT NULL DEFAULT 'Applied',
    applied_at          TEXT,
    source              TEXT,
    next_follow_up_at   TEXT,
    last_updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS application_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id  INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    type            TEXT NOT NULL,
    content         TEXT,
    timestamp       TEXT NOT NULL,
    raw_sms         TEXT
);

CREATE TABLE IF NOT EXISTS context_memory (
    user_id             TEXT PRIMARY KEY,
    last_company        TEXT,
    last_role           TEXT,
    last_application_id INTEGER,
    updated_at          TEXT
);

-- One in-flight multi-turn exchange per user. pending_intent is the action we
-- are collecting slots for; awaiting is the single slot we last asked about.
CREATE TABLE IF NOT EXISTS conversation_state (
    user_id         TEXT PRIMARY KEY,
    pending_intent  TEXT,
    slots           TEXT,       -- JSON blob of collected slot values
    awaiting        TEXT,       -- slot name or 'confirm'
    updated_at      TEXT
);

-- Scheduled reminders. Delivery is decoupled: a reminder is just a row with a
-- due time; a sender (log now, Twilio outbound once A2P clears) ships it.
CREATE TABLE IF NOT EXISTS reminders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    application_id  INTEGER REFERENCES applications(id) ON DELETE SET NULL,
    remind_at       TEXT NOT NULL,
    body            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | sent | cancelled
    created_at      TEXT NOT NULL,
    sent_at         TEXT
);

-- Recruiters / hiring contacts discovered for a company (Phase 3, Apollo). A
-- company can have several; we dedupe on (user_id, company, name). application_id
-- links them to a specific application when known, but recruiters are keyed by
-- company so they survive an application being deleted.
CREATE TABLE IF NOT EXISTS recruiters (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    application_id  INTEGER REFERENCES applications(id) ON DELETE SET NULL,
    company         TEXT NOT NULL,
    name            TEXT NOT NULL,
    title           TEXT,
    email           TEXT,
    linkedin_url    TEXT,
    source          TEXT NOT NULL DEFAULT 'apollo',  -- apollo | manual
    apollo_person_id TEXT,
    created_at      TEXT NOT NULL
);

-- Concrete dated events tied to an application (OA due, interview, onsite). A
-- deadline is a calendar item you want to see *before* it lands; the upcoming
-- view reads this. Setting one also schedules a heads-up via the reminders
-- pipeline, so notification reuses the same sender (log now, Twilio later).
CREATE TABLE IF NOT EXISTS deadlines (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    application_id  INTEGER REFERENCES applications(id) ON DELETE SET NULL,
    company         TEXT NOT NULL,
    label           TEXT NOT NULL,        -- e.g. "OA", "Interview", "Onsite", "Deadline"
    due_at          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',  -- open | done
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_apps_user ON applications(user_id);
CREATE INDEX IF NOT EXISTS idx_events_app ON application_events(application_id);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, remind_at);
CREATE INDEX IF NOT EXISTS idx_recruiters_company ON recruiters(user_id, company);
CREATE INDEX IF NOT EXISTS idx_deadlines_due ON deadlines(user_id, status, due_at);

-- Apollo API call log (for daily caps + /health visibility). people_search uses
-- api_search (no credits); org_search uses mixed_companies/search (credits).
CREATE TABLE IF NOT EXISTS apollo_api_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    call_type   TEXT NOT NULL,
    company     TEXT,
    called_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_apollo_calls_at ON apollo_api_calls(called_at);

-- Optional domain cache when APOLLO_ORG_LOOKUP_ENABLED=true (avoids repeat org searches).
CREATE TABLE IF NOT EXISTS company_domains (
    company_key     TEXT PRIMARY KEY,
    domain          TEXT NOT NULL,
    apollo_org_id   TEXT,
    resolved_at     TEXT NOT NULL
);

-- Negative cache: org search found no domain — skip repeat credit spend for a while.
CREATE TABLE IF NOT EXISTS company_domain_misses (
    company_key TEXT PRIMARY KEY,
    tried_at    TEXT NOT NULL
);

-- Single-level undo. One row per user holding just the *most recent* reversible
-- action; each new mutation overwrites it. `payload` is JSON carrying whatever
-- the reversal needs (prior status, created event id, etc.). A 'delete' kind is
-- recorded as a tombstone so "undo" can honestly say a delete can't be reversed
-- rather than silently undoing the action before it.
CREATE TABLE IF NOT EXISTS undo_log (
    user_id     TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,   -- apply | status | note | edit | bulk | delete
    payload     TEXT NOT NULL,   -- JSON reversal data
    summary     TEXT NOT NULL,   -- human description of what would be undone
    created_at  TEXT NOT NULL
);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(get_settings().database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate_schema(conn)


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Add columns introduced after first deploy (idempotent)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(recruiters)")}
    if "apollo_person_id" not in cols:
        conn.execute("ALTER TABLE recruiters ADD COLUMN apollo_person_id TEXT")
    domain_cols = {r[1] for r in conn.execute("PRAGMA table_info(company_domains)")}
    if domain_cols and "apollo_org_id" not in domain_cols:
        conn.execute("ALTER TABLE company_domains ADD COLUMN apollo_org_id TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_recruiters_apollo_person "
        "ON recruiters(user_id, company, apollo_person_id) "
        "WHERE apollo_person_id IS NOT NULL"
    )
