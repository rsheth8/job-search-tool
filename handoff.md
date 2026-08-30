# JobPilot — Engineering Handoff

> Pick-up doc for a fresh session. Last updated **2026-08-27**.
> **Invite-only iOS beta:** Sign in with Apple, session-scoped data, first-run setup,
> TestFlight. See [`deploy/BETA.md`](deploy/BETA.md).

---

## TL;DR

A **personal, conversational job-search engine** with an **invite-only iOS beta**.
Testers sign in with Apple, finish a setup wizard (roles → identity → one project),
then discover → prepare → ⚡ Autofill in the in-app browser → **Submit themselves**.
In-app chat is the assistant; JSON apply APIs power the iOS Apply tab.

**Isolation:** JSON APIs prefer the Bearer session and ignore `?user=` when auth is
on. Production sets `AUTH_FAIL_OPEN=false`. `AUTH_ALLOWED_EMAILS` is the invite list.
User ids are `usr_…` (Apple sign-in).

**Autofill:** iOS WebView only. Rules come from `GET /apply/rules` (`fieldmatch.py`).
Human always clicks Submit. Résumé attach is manual (WKWebView cannot set file inputs).

---

## 1. Stack & deployment

- **App:** FastAPI + SQLite. Python 3.13. Entry: `uvicorn app.main:app`.
- **LLM:** Anthropic **Claude Haiku 4.5** only (`claude-haiku-4-5`). Every paid call
  is gated, rate-limited, daily-capped, and **fail-open** to a free heuristic/template.
  CI never hits a network or real model.
- **Transport:** **In-app chat** (`POST /chat`, Bearer session) + iOS Apply tab.
  Engine is transport-agnostic; CLI for local dev.
- **Alerts & reminders:** `AppSender` — appends to chat transcript + best-effort APNs.
- **Hosting:** **Fly.io**, app `job-search-tool`, region `iad`, URL
  `https://job-search-tool.fly.dev`. One always-warm 512MB machine (in-process
  APScheduler for reminders + discovery). SQLite on a **1GB persistent volume at
  `/data/job_search.db`**. Redeploy: push to `main` (CI pytest, then Fly deploy) or
  `flyctl deploy -a job-search-tool`. Migrations are idempotent (run at import).
- **Local venv:** use `.venv/bin/python` directly (repo pins 3.13).

---

## 2. Subsystems

### A. Conversational CRM
Two-stage router (heuristic regex → Claude fallback) + slot-filling engine.
Intents: track/apply/update/check/edit/bulk/delete/undo, reminders, deadlines,
stats, recent-window queries, multi-action, smalltalk, knowledge (`remember …`).
Two-step confirms on destructive ops; single-level undo.
Files: `router.py`, `engine.py`, `intents.py`, `conversation.py`, `store.py`,
`scoring.py`, `reminders.py`, `deadlines.py`, `stats.py`, `importer.py`.

### B. Job discovery
Background scheduler runs `discovery.tick` per user. Wide sourcing: free ATS
(Greenhouse/Lever/Ashby), RSS feeds, rotating ATS directory (~60 boards), and the
Pitt CSC / Simplify internship list. Pipeline per posting:
`fetch → dedupe → quality → ghost filter → eligibility gate → prefilter+cap →
score → re-rank → threshold → alert (digest/instant/silent)`.
Chat actions: `apply N`, `queue N`, `dismiss N`, `snooze N`, `be less picky` (TUNE).
Files: `discovery.py`, `wide_discovery.py`, `jobsources/`, `jobstore.py`, `profile.py`,
`matcher.py`, `quality.py`, `posting_match.py`, `job_alerts.py`, `jobs_review.py`.

### C. Matching (heuristic / LLM / reranker / eligibility / ghost)
- **Heuristic + LLM score:** keyword/location pre-filter, then Haiku batch score (or
  free heuristic without a key).
- **Personalized re-ranker** (`reranker.py`, **off by default**): pure-Python L2
  logistic regression on your apply/dismiss/swipe labels. Features: relevance,
  kw_overlap, title_hit, loc_match, is_remote, first_party.
  Hold-out promotion guard; outcome-graded labels.
- **Ghost filter + eligibility gate:** rules-based, free, on by default.

### C2. Personal knowledge (`app/knowledge.py`)
Projects, achievements, strengths, preferences, reusable answers. `knowledge_block()`
grounds drafting; saved answers matching a question return verbatim (no model call).
Teach via chat: `remember project: …`, `remember answer to "Why us?": …`.
Inspect: `what do you know about me` (+ identity-coverage audit).

### D. Application assistance
- **Apply queue** (`apply_queue.py`, JSON `/apply/*`): stage a posting (`queue N`,
  `queue top 3`, or auto-queue above threshold). Assembles apply link + per-question
  tailored answers + tailored resume + identity. **Never submits** — human submits
  on the ATS site.
- **Applicant identity** (`applicant.py`): name/email/phone/links/location/work-auth/
  education/etc. EEO/demographic fields excluded from autofill. Canonical
  `referral_source` keys: `company career site` | `job board` | `linkedin` |
  `referral` | `recruiter` | `event`.
- **Resume tailoring** (`resume_tailor.py`): per-posting one-page PDF (SWE vs AI/ML),
  Tectonic-compiled, cached. Base `.tex` on volume (see §5).

### D2. iPhone app — `ios/` (SwiftUI + WKWebView)
Tabs: **Apply** · **About** · **Chat** · **Settings**. Apply opens the real form;
⚡ Autofill fills identity + drafted answers (Greenhouse react-select, Ashby Yes/No
buttons). Push (APNs) on new matches. See `ios/README.md`.

#### D2a. The two rules the fill engine lives by
`Autofill.swift`'s JS is injected into **every frame** (`forMainFrameOnly: false`),
and every frame shares one native message handler. So:

1. **Only the top frame reports.** Subframes answer their parent's ping over
   `postMessage` (ack first, then the result — a real ATS fill can take 20s) and
   the top frame adds up the totals. Break this and an `about:blank` or reCAPTCHA
   frame's `filled: 0` lands after the real result: the toast says "No fields
   matched" over a filled form, and `skips` — the signal `filllearn` grows the
   phrasing table from — is wiped on exactly the embedded forms that need it.
2. **Fill blanks, never overwrite** (`hasOwnValue`). A second ⚡ tap or an
   autopilot step revisiting a mounted field must not undo an answer the person
   typed. A *declared* combobox (role/aria-autocomplete/aria-controls) that
   matches no option is cleared, not typed into: text in the box with nothing
   committed is a form that looks complete and submits empty.

Blockers are judged by what a person can actually see. An invisible, score-based
reCAPTCHA (Greenhouse) or a zero-height rendered hCaptcha (Lever) is not a wall;
a "Sign in" link in the site nav is not a wall. `tests/test_ios_autofill.py`
pins each of these against fixtures modelled on the live DOM.

### E. Autofill rules — `app/fieldmatch.py`
Single source of truth for label→identity matching, select/Yes-No matching, EEO
never-fill. Served to iOS via `GET /apply/rules`; bundled fallback in
`Autofill.swift` for offline. `tests/test_ios_autofill.py` + `tests/test_rules_parity.py`.

Notable identity keys: `referral_source` (synonym table → company wording),
`previously_employed` (bool → Yes/No via `_yes_no_option`, refuses ambiguous pairs).

---

## 3. Key endpoints

- `GET /health` (flags + discovery stats)
- `POST /auth/apple` · `GET /auth/me` · `POST /auth/logout`
- `GET /chat/history` · `POST /chat`
- `GET /apply/data` · `POST /apply/stage|package|mark|applied|remove|pass`
- `GET /apply/resume` (PDF) · `GET /apply/cover` (optional letter) · `POST /apply/answer/save|redraft`
- `GET/POST /apply/identity` · `POST /apply/answer` (single question)
- `GET /apply/rules` (autofill) · `GET/POST /apply/knowledge` · `POST /apply/knowledge/remove`
- `GET /apply/setup` · `GET/POST /apply/profile`
- `POST /apply/device` (+ `/remove`) · `POST /feedback`

---

## 4. User identity & data

- Apple sign-in mints `usr_…` ids. Optional one-time `AUTH_LEGACY_USER_ID` folds an
  old dev/Slack-keyed row into the new account on first login, then unset it.
- **`JOB_ALERT_USER`:** your `usr_…` (or empty → busiest user). Digests land in chat.
- **`FEEDBACK_NOTIFY_USER`:** your `usr_…` — feedback pings you in chat.
- Tools: `scripts.migrate_user`, `scripts.export_user`, `scripts.import_user` (move a
  "brain" across DBs). On Fly: `flyctl ssh console -C "cd /app && python -m scripts.X …"`.

---

## 5. Outstanding beta items

1. **Fly secrets** (see `deploy/BETA.md`): `APPLY_API_TOKEN`, `AUTH_ALLOWED_EMAILS`,
   `APPLE_CLIENT_IDS`, `SENTRY_DSN`, valid `ANTHROPIC_API_KEY`.
2. **Optional brain:** `RERANKER_ENABLED=true` (personalized
   features for reranker + per-question answers). Import trained labels via
   `scripts.import_user` if migrating from local dev.
3. **Base resumes** on volume: `swe.tex` + `aiml.tex` → `/data/resumes/`.
4. **Push:** `PUSH_ENABLED` + all `APNS_*`; `APNS_USE_SANDBOX=false` for TestFlight.
5. **Dogfood:** one real Greenhouse/Lever/Ashby apply via Autofill; résumé manual attach.

---

## 6. Config flags (defaults in `app/config.py`)

`RERANKER_ENABLED` (false) · `RERANKER_OUTCOME_WEIGHTING` (true) ·
(false) · `GHOST_FILTER_ENABLED` (true) · `ELIGIBILITY_FILTER_ENABLED` (true) ·
`JOB_RELEVANCE_THRESHOLD` (0.6) ·
`JOB_AUTO_QUEUE_THRESHOLD` (0.0 = off) · `JOB_ALERT_MODE` (digest) ·
`JOB_ALERT_USER` ("") · `RESUME_TAILOR_ENABLED` (true) · `APPLY_API_TOKEN` ("") ·
`PUSH_ENABLED` (false) + `APNS_*` · auth allowlist flags.

---

## 7. Run / test locally

```bash
.venv/bin/python -m pytest -q          # full suite (~700 tests)
.venv/bin/uvicorn app.main:app --reload
```

`tests/conftest.py` forces offline mode and uses a temp DB. Flag overrides need
`monkeypatch.setenv(...)` then `config.get_settings.cache_clear()`.

---

## 8. Known limitations

- **Résumé upload from iOS autofill** is not possible — attach manually or use the
  pre-downloaded PDF share sheet in the apply browser.
- **Login-gated sites** (Workday, LinkedIn Easy Apply) are out of scope for public-form
  autofill.
- **Exotic custom dropdowns** may still need a manual tap; ARIA comboboxes generally work.
- **Reranker** needs enough swipe labels before enabling; LLM fit features need
- **In-app browser holds one form at a time** — popping back destroys the WebView state.

---

## 9. Gotchas

- **`.env.example` is tracked** — never put live secrets there.
- **`scripts/` must stay OUT of `.dockerignore`** (operational scripts on Fly).
- **Use `.venv/bin/python`**, not bare `python`.
- **Brain export:** `posting_summaries` is keyed by `source:external_id` — re-export
  after `usermerge.py` fixes if restoring an old brain.
- **Run scripts on Fly from `/app`:** SSH lands in `/`; `cd /app && python -m scripts.X`.
