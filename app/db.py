"""SQLite access layer.

Plain sqlite3 (no ORM) keeps the dependency surface small and the queries
readable. Connections are short-lived and created per call, and no transaction
here spans a network call — which is what makes one file on one volume a
defensible choice for a small multi-user deployment rather than a liability.

The concurrency that has to work: uvicorn runs sync route handlers in a thread
pool, and APScheduler runs the reminder and discovery loops on their own threads
in the *same* process. So there are genuinely concurrent readers and writers, and
under the default rollback journal a writer takes an exclusive lock on the whole
database — blocking readers, not just other writers. WAL is what makes those two
groups stop fighting; see ``_configure``.
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

-- Job boards we poll for new openings. (source, board_token) identifies the
-- board to query; company_name is for display. One row per board the user
-- tracks; deduped per (user_id, source, board_token).
CREATE TABLE IF NOT EXISTS tracked_companies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL,
    source        TEXT NOT NULL,   -- greenhouse | lever | ashby | ...
    board_token   TEXT NOT NULL,   -- board slug used to query the source
    company_name  TEXT,            -- display name
    created_at    TEXT NOT NULL
);

-- Job postings discovered from tracked boards. Deduped on
-- (user_id, source, external_id) so a posting is scored + alerted once, ever.
-- status walks seeded -> new (weak) / queued (match) -> applied|dismissed;
-- alerted is used only in instant alert mode. relevance_score is the
-- matcher's 0..1 fit score against the user's profile.
CREATE TABLE IF NOT EXISTS job_postings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    source          TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    company         TEXT,
    title           TEXT,
    location        TEXT,
    url             TEXT,
    description     TEXT,
    posted_at       TEXT,
    first_seen_at   TEXT NOT NULL,
    relevance_score REAL,
    status          TEXT NOT NULL DEFAULT 'new',  -- new|alerted|applied|dismissed|snoozed|seeded
    snoozed_until   TEXT,                         -- when a 'snoozed' posting should resurface
    sort_order      INTEGER,                       -- user rank on Apply; NULL = score order
    embedding       BLOB                          -- float32 JD vector (Matching v2); NULL when off
);

-- One job-search profile per user: target roles/keywords/locations plus a short
-- resume summary. Drives LLM relevance scoring and (Phase 2) apply drafts.
CREATE TABLE IF NOT EXISTS job_search_profile (
    user_id        TEXT PRIMARY KEY,
    roles          TEXT,            -- comma-separated target role keywords
    keywords       TEXT,            -- comma-separated must-have/nice-to-have terms
    locations      TEXT,            -- comma-separated preferred locations / "remote"
    seniority      TEXT,            -- e.g. "new grad", "senior"
    resume_summary TEXT,            -- a few lines describing the candidate
    prefs_json     TEXT,            -- free-form JSON for future prefs
    min_relevance  REAL,            -- per-user alert threshold (NULL = use global default)
    applicant_json TEXT,            -- JSON identity for application autofill (name/email/links/work-auth)
    updated_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_tracked_user ON tracked_companies(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tracked_dedupe
    ON tracked_companies(user_id, source, board_token);
CREATE UNIQUE INDEX IF NOT EXISTS idx_postings_dedupe
    ON job_postings(user_id, source, external_id);
CREATE INDEX IF NOT EXISTS idx_postings_user_status
    ON job_postings(user_id, status);

-- Rotating cursor for ATS directory wide discovery (global round-robin).
CREATE TABLE IF NOT EXISTS discovery_cursors (
    cursor_key    TEXT PRIMARY KEY,
    position      INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL
);

-- Board tokens learned from apply URLs (swelist / RSS / YC) and merged into
-- the rotating ATS directory. Distinct from the curated JSON file.
CREATE TABLE IF NOT EXISTS directory_learned_boards (
    source        TEXT NOT NULL,
    board_token   TEXT NOT NULL,
    learned_at   TEXT NOT NULL,
    PRIMARY KEY (source, board_token)
);

-- Tailored resume cache: PDF + .tex on the Fly volume, indexed here for reuse.
CREATE TABLE IF NOT EXISTS tailored_resumes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    cache_key   TEXT NOT NULL,
    company     TEXT,
    title       TEXT,
    variant     TEXT NOT NULL,
    pdf_path    TEXT NOT NULL,
    tex_path    TEXT NOT NULL,
    posting_id  INTEGER,            -- optional link to job_postings.id when known
    pages       INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    UNIQUE(user_id, cache_key)
);

CREATE INDEX IF NOT EXISTS idx_tailored_user_company
    ON tailored_resumes(user_id, variant, company);
CREATE INDEX IF NOT EXISTS idx_tailored_posting
    ON tailored_resumes(user_id, posting_id);

-- Personalized re-ranker model (Matching v2, Phase 2). One small logistic-
-- regression model per user, stored as JSON (weights + bias + metadata),
-- retrained from the user's apply/dismiss/snooze labels as they accumulate.
CREATE TABLE IF NOT EXISTS reranker_models (
    user_id     TEXT PRIMARY KEY,
    model_json  TEXT NOT NULL,
    n_labels    INTEGER NOT NULL,
    trained_at  TEXT NOT NULL
);

-- Swipe-trainer labels: fast 'would I apply?' yes/no judgements on real postings,
-- used to bootstrap the re-ranker before the user has applied to much. Kept
-- separate from job_postings so it never touches the real application pipeline.
CREATE TABLE IF NOT EXISTS training_labels (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    source          TEXT NOT NULL,
    external_id     TEXT NOT NULL,
    company         TEXT,
    title           TEXT,
    location        TEXT,
    url             TEXT,
    description     TEXT,
    relevance_score REAL,
    label           TEXT NOT NULL,   -- 'like' (would apply) | 'pass'
    created_at      TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_training_labels_dedupe
    ON training_labels(user_id, source, external_id);

-- Cached plain-language summaries for swipe cards (one batched Haiku call fills
-- many; cached by posting so a role is summarized once ever — keeps AI spend low).
-- The summary is a JSON blob so new fields don't need a migration.
CREATE TABLE IF NOT EXISTS posting_summaries (
    cache_key    TEXT PRIMARY KEY,   -- "{source}:{external_id}"
    summary_json TEXT NOT NULL,      -- {tldr, level, skills, fit}
    created_at   TEXT NOT NULL
);

-- Semi-auto application queue: postings staged to apply, with a pre-assembled
-- package (draft answers + tailored resume). Status walks staged -> ready ->
-- submitted; the human always clicks Submit in the iOS WebView.
-- Personal knowledge: the durable facts about the user that make an application
-- answer specific rather than generic — projects, achievements, strengths, work
-- preferences, and reusable canned answers to questions every ATS asks. Grounds
-- the answer drafter, and lets a canned answer be reused with no LLM call at all.
CREATE TABLE IF NOT EXISTS user_knowledge (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    category   TEXT NOT NULL,   -- experience | project | achievement | strength | preference | answer
    label      TEXT,            -- for 'answer': the question it answers
    text       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_user_knowledge_user ON user_knowledge(user_id, category);

-- APNs device tokens for the iPhone app. iOS hands the app the same token on every
-- launch, so registration upserts rather than duplicating; a token APNs reports as
-- dead (app deleted / wrong environment) is dropped on the spot.
CREATE TABLE IF NOT EXISTS device_tokens (
    user_id    TEXT NOT NULL,
    token      TEXT NOT NULL,
    platform   TEXT NOT NULL DEFAULT 'ios',
    timezone   TEXT,                          -- IANA id from the phone (push greetings)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, token)
);

CREATE TABLE IF NOT EXISTS apply_queue (
    user_id      TEXT NOT NULL,
    posting_id   INTEGER NOT NULL REFERENCES job_postings(id) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT 'staged',  -- staged | ready | submitted
    answers      TEXT,                            -- legacy single "why I'm a fit" blurb
    questions_json TEXT,                          -- cached [{question, answer}] per posting
    resume_path  TEXT,                            -- cached tailored-resume PDF path
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    sort_order   INTEGER,                          -- user rank; lower is sooner (Next)
    PRIMARY KEY (user_id, posting_id)
);

-- App accounts (Sign in with Apple). ``id`` is the opaque user_id used everywhere
-- else in the DB; ``apple_sub`` is Apple's stable subject. ``legacy_user_id``
-- records a Slack/phone id that was merged into this account on first sign-in.
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    apple_sub       TEXT UNIQUE,
    email           TEXT,
    display_name    TEXT,
    -- scrypt hash for email accounts; NULL for Sign in with Apple rows.
    -- Also ALTERed in for existing databases, like every other column here.
    password_hash   TEXT,
    legacy_user_id  TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_legacy ON users(legacy_user_id);

-- Opaque session tokens (stored hashed). Bearer auth for chat + apply.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

-- Durable chat transcript for the in-app / web agent. Separate from
-- conversation_state (which only holds in-flight slot-filling).
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    role       TEXT NOT NULL,   -- user | assistant
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_user ON chat_messages(user_id, id);

-- Per-user paid LLM call log (daily cap). One row per consume().
-- ``feature`` slices the cap so one caller can't spend the whole day (see
-- app.llm_budget). Rows written before that column existed carry ''.
CREATE TABLE IF NOT EXISTS llm_usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    day        TEXT NOT NULL,
    feature    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_user_day ON llm_usage(user_id, day);
-- idx_llm_usage_feature is created in _migrate_schema, not here: this script
-- runs before the migration that adds `feature`, so on an existing database
-- the index would reference a column that does not exist yet.

-- Invite-only beta feedback from the iOS app.
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT NOT NULL,
    body       TEXT NOT NULL,
    context    TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id, id);

-- Labels Autofill skipped (unmatched wording, empty identity, no listed option).
-- Grows the phrasing table from jobs the user actually applies to.
CREATE TABLE IF NOT EXISTS fill_skips (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    label       TEXT NOT NULL,
    label_norm  TEXT NOT NULL,
    reason      TEXT NOT NULL,
    key         TEXT,
    url         TEXT,
    posting_id  INTEGER,
    count       INTEGER NOT NULL DEFAULT 1,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    options     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fill_skips_dedupe
    ON fill_skips(user_id, label_norm, reason);
CREATE INDEX IF NOT EXISTS idx_fill_skips_user ON fill_skips(user_id, last_seen);
"""


# How long a connection waits for a lock before raising "database is locked".
# Python's default is 5s. Writes here are short (no transaction spans a network
# call), so anything approaching this ceiling means something is wrong rather
# than merely busy -- but a scheduler tick and a phone refresh landing together
# should queue, never fail.
BUSY_TIMEOUT_MS = 5000


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(get_settings().database_path,
                           timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Cheap (a no-op read once the file is already in WAL) and per-connection,
    # unlike journal_mode which is a property of the file.
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _configure(conn: sqlite3.Connection) -> None:
    """One-time, file-level settings. Both survive in the database itself.

    ``journal_mode=WAL`` lets readers and one writer proceed at the same time.
    Without it SQLite uses a rollback journal, where a writer holds an EXCLUSIVE
    lock over the entire file: with one user that is invisible, and with a dozen
    it is the scheduler's discovery pass making every phone in the beta wait on
    it. WAL is a property of the database file, so setting it once here applies
    to every connection afterwards, including ones opened by scripts.

    ``synchronous=NORMAL`` is the standard companion. In WAL it still cannot
    corrupt the database on a crash; it trades the possibility of losing the
    last commits on *power loss* for not fsyncing on every one of them. On a
    Fly volume that is the right trade for a write-heavy discovery loop.
    """
    # WAL needs shared memory, which some filesystems (and :memory:) don't
    # provide. Falling back to the default is correct there, not fatal.
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.DatabaseError:  # pragma: no cover - filesystem-dependent
        pass


def journal_mode() -> str:
    """The database file's current journal mode, for /health and tests."""
    with connect() as conn:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]


def init_db() -> None:
    with connect() as conn:
        _configure(conn)
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
    # Discovery polish: snooze timestamp on postings + per-user alert threshold.
    post_cols = {r[1] for r in conn.execute("PRAGMA table_info(job_postings)")}
    if post_cols and "snoozed_until" not in post_cols:
        conn.execute("ALTER TABLE job_postings ADD COLUMN snoozed_until TEXT")
    if post_cols and "embedding" not in post_cols:
        conn.execute("ALTER TABLE job_postings ADD COLUMN embedding BLOB")
    prof_cols = {r[1] for r in conn.execute("PRAGMA table_info(job_search_profile)")}
    if prof_cols and "min_relevance" not in prof_cols:
        conn.execute("ALTER TABLE job_search_profile ADD COLUMN min_relevance REAL")
    if prof_cols and "applicant_json" not in prof_cols:
        conn.execute("ALTER TABLE job_search_profile ADD COLUMN applicant_json TEXT")
    aq_cols = {r[1] for r in conn.execute("PRAGMA table_info(apply_queue)")}
    if aq_cols and "questions_json" not in aq_cols:
        conn.execute("ALTER TABLE apply_queue ADD COLUMN questions_json TEXT")
    if aq_cols and "sort_order" not in aq_cols:
        conn.execute("ALTER TABLE apply_queue ADD COLUMN sort_order INTEGER")
    if post_cols and "sort_order" not in post_cols:
        conn.execute("ALTER TABLE job_postings ADD COLUMN sort_order INTEGER")
    dev_cols = {r[1] for r in conn.execute("PRAGMA table_info(device_tokens)")}
    if dev_cols and "timezone" not in dev_cols:
        conn.execute("ALTER TABLE device_tokens ADD COLUMN timezone TEXT")
    # posting_summaries moved from (tldr, fit) columns to a JSON blob; the old rows
    # are a regenerable cache, so just rebuild the table on the richer schema.
    sum_cols = {r[1] for r in conn.execute("PRAGMA table_info(posting_summaries)")}
    if sum_cols and "summary_json" not in sum_cols:
        conn.execute("DROP TABLE posting_summaries")
        conn.execute(
            "CREATE TABLE posting_summaries (cache_key TEXT PRIMARY KEY, "
            "summary_json TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
    fb_cols = {r[1] for r in conn.execute("PRAGMA table_info(feedback)")}
    if fb_cols and "context" not in fb_cols:
        conn.execute("ALTER TABLE feedback ADD COLUMN context TEXT")
    # Email + password accounts alongside Sign in with Apple. Apple rows keep a
    # NULL password_hash; the partial unique index only constrains the email
    # accounts, so existing Apple users with a shared/NULL email are untouched.
    user_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if user_cols and "password_hash" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_password "
        "ON users(lower(email)) WHERE password_hash IS NOT NULL"
    )
    usage_cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_usage)")}
    if usage_cols and "feature" not in usage_cols:
        conn.execute(
            "ALTER TABLE llm_usage ADD COLUMN feature TEXT NOT NULL DEFAULT ''"
        )
    # Unconditional, and after the ALTER: a fresh database gets `feature` from
    # SCHEMA and an existing one gets it from the line above, so this is the one
    # place that creates the index and both paths end up with the same shape.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_usage_feature "
        "ON llm_usage(user_id, day, feature)"
    )
