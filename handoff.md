# Job Search Intelligence — Engineering Handoff

> Status doc for picking up the project in a fresh session. Reflects what's
> **actually built**, not just the original spec. Last updated 2026-05-30
> (**Slack is now the primary transport** — Events API inbound + Web API
> outbound, reusing the same engine; Twilio kept dormant. Plus the prior layer:
> backfill import, stats, deadlines, web dashboard, menu, and a conversational
> assistant layer: smalltalk + CHECK / DELETE / EDIT / BULK with two-step
> confirmation on destructive actions).
>
> **Latest pass (2026-05-30, this session):** conversational depth + polish +
> Fly.io deploy scaffolding. Added **UNDO** (single-level reverse of the last
> apply/status/note/edit/bulk; delete is a tombstone — honestly "can't undo"),
> **relative-date LIST** ("what did I apply to this week / today / last month /
> since monday"), and **multi-turn EDIT** ("change databricks" → asks what →
> "role to SWE II"). Made `next_follow_up_at` live (recomputed on every activity,
> cleared for closed apps; surfaced in CHECK) — closes the one code TODO. Synced
> README. Added `Dockerfile` + `fly.toml` + `deploy/README.md` (Fly runbook,
> Docker-build + volume-persistence validated locally). Tests 198 → **234**
> (`test_undo`, `test_recent`, `test_edit_flow`, `test_followup_persist`).

**Transport note:** the original spec was SMS-first via Twilio, but Twilio stalled
on A2P 10DLC, so messaging moved to **Slack** (no carrier approval, free, outbound
works immediately). The brain (`engine.handle_sms`) is transport-agnostic — Slack
and Twilio are interchangeable front-ends. Docs below still say "SMS" in places;
read that as "the messaging channel," now Slack.

---

## 1. What this is

A **personal, SMS-first job-application tracker**. You text it natural messages
("applied spotify swe ii", "spotify oa received", "what should I follow up on")
and it logs applications, updates statuses, takes notes, and holds a real
multi-turn conversation. It is a **high-speed personal execution engine for
maximizing job interviews**, not a SaaS product.

**Design philosophy (load-bearing):**
- Optimize for *speed of logging + completeness of tracking + minimal effort*.
- **Never block the user.** Always store something useful or ask exactly one
  short question. Never hard-fail.
- Prefer capturing intent over perfect parsing. Infer, store, ask if needed.
- Not optimizing for: UI polish, scale, enterprise architecture.

**Mental model:** a *repairable conversational CRM that lives inside SMS*.

---

## 2. Current status (what's done)

**All three phases built + a productivity layer (backfill, stats, deadlines,
web dashboard, conversational assistant layer) + a Slack transport + Fly.io
deploy scaffolding — tested (234 passing tests).** Messaging now runs over
**Slack** (inbound replies + outbound
reminders both live once tokens are set). Twilio is intact but dormant (still
blocked on A2P 10DLC). Everything is usable today via Slack, CLI, and browser
dashboard.

### Slack transport (primary)

- `app/slack.py` — `verify_signature()` (Slack v0 HMAC, replay-guarded),
  `SlackSender` (implements `reminders.Sender`), `post_message()` (httpx, never
  raises), `handle_event()` (dispatch → `handle_sms` → reply). httpx only, no
  `slack_sdk`. `user_id` = Slack user ID (DMs via `chat.postMessage(channel=uid)`).
- `POST /slack/events` (`app/main.py`) — URL-verification challenge, signature
  check, **background-task dispatch** (acks in <3s so Slack never retries). Bot/
  edited/own messages ignored (loop guard); events deduped by `event_id`.
- Config: `SLACK_BOT_TOKEN` (xoxb-, enables inbound+outbound), `SLACK_SIGNING_SECRET`
  (validates inbound). `settings.slack_enabled`. `get_sender()` precedence:
  **Slack → Twilio → LogSender**. `/health` reports `reminder_delivery: slack`.
- Conftest neutralizes `SLACK_*` env so the live `.env` tokens don't leak into
  tests (webhook tests post unsigned; no test calls the Slack API).
- Smoke-tested end-to-end through `/slack/events`: challenge echo, two DMs logged
  + replied, bot-loop guard, dedupe, persistence — all confirmed.

### Live integrations (`.env`, gitignored)

| Integration | Status | Notes |
|---|---|---|
| **Claude router** | LIVE | `ANTHROPIC_API_KEY` set, Haiku 4.5. Tests force offline heuristic. |
| **Apollo OUTREACH** | LIVE | Paid **Basic** plan + master API key. People search works. |
| **Slack** | LIVE (primary) | Events API inbound + Web API outbound. `SLACK_BOT_TOKEN` + `SLACK_SIGNING_SECRET` set; webhook at `/slack/events`. |
| **Twilio inbound** | Dormant | TwiML replies still work; superseded by Slack. Re-wire number if ever needed. |
| **Twilio outbound** | OFF | `TWILIO_AUTH_TOKEN` set; SID + FROM unset. A2P pending; Slack is the active outbound path. |
| **Signature validation** | Twilio OFF / Slack ON | `twilio_validate_signature=false`; Slack requests verified when `SLACK_SIGNING_SECRET` set. |

### Working today

- SMS ingestion via FastAPI webhook (Twilio-compatible TwiML reply).
- Intent router: **`HeuristicRouter`** (offline) + **`AnthropicRouter`** (Claude Haiku 4.5, structured JSON). Auto-pick by API key; LLM rate-limit → heuristic fallback.
- Intents: APPLY, UPDATE, NOTE, LIST, QUERY, STATS, DEADLINE, CHECK, DELETE, EDIT, BULK, UNDO, REMIND, OUTREACH, UNKNOWN — all wired.
- **Undo (UNDO)**: "undo" / "revert" / "take that back" → single-level reverse of the most recent reversible action (apply→delete, status→prior stage, note→drop event, edit→restore fields, bulk→restore all touched). Each `_do_*` records an undo row (`store.record_undo`); `_do_undo` reverses + deletes the recorded event so history stays honest. DELETE writes a **tombstone** so undo says "can't undo a delete" rather than silently undoing the prior action. Single-level only (next undo = "nothing to undo").
- **Relative-date LIST**: "what did I apply to this week / today / yesterday / last month / this year / since monday / past 7 days" → `_apply_window()` maps the phrase to an `applied_at` range, `store.applications_in_window()` filters. Router sets `time_reference` on LIST; `_RECENT_RE` catches these before APPLY/CHECK can grab the word "apply".
- **Multi-turn EDIT**: EDIT is now slot-filling — "change databricks" asks "what should I change (role/name/date)?" and threads the bare follow-up ("role to SWE II", "call it Acme Inc") back via pending `awaiting="edit_change"` (`_start_edit`/`_continue_edit`/`_apply_edit`, `router.parse_edit_change`). One-shot edits still work.
- **Assistant feel**: smalltalk (thanks/ack/compliment/sign-off → warm replies, not the confused fallback); CHECK ("what's the status of stripe", "where do I stand with google", "do I have notion") → rich per-app summary (stage, last activity, latest note, next deadline); DELETE ("delete stripe", "I never applied to ramp") → confirm-then-remove. Smalltalk only fires with no pending exchange + UNKNOWN intent, so a mid-confirm "ok" still means yes.
- **Corrections (EDIT)**: "change the stripe role to SWE II", "stripe is actually a PM role", "rename databricks to Databricks Inc", applied-date fixes → updates a stored entry in place (writes an 'edit' event). NOT destructive → applies directly. "change X to <stage>" is routed to UPDATE, not EDIT.
- **Bulk/relative updates (BULK)**: "reject everything still in Applied", "ghost anything older than 30 days", "mark all applied as ghosted" → mass stage change with current-stage + age filters. **Always two-step**: shows the exact count + sample companies and applies only on an explicit "yes"; terminal apps are never swept. Field convention (heuristic + LLM): status=new stage, message=current-stage filter, time_reference=age.
- **Destructive = two-step everywhere**: DELETE and BULK both require a confirm that states the consequence ("can't be undone") before acting.
- **Multi-turn conversation** (slot filling, confirmations, corrections, duplicate guard, 30-min expiry).
- **Multi-action** combined messages (LLM-only; up to 4 actions).
- **Phase 2:** follow-up scoring (`app/scoring.py`) + reminder scheduling (APScheduler). REMIND persists rows; delivery via `LogSender` until Twilio outbound enabled.
- **Phase 3:** Apollo recruiter discovery + outreach drafting. Live-tested on Stripe (returns names/titles + Claude draft). **No auto-send** — user copy/pastes.
- **Apollo credit guardrails** (see §4) — limits, caching, SMS footnotes, org lookup off by default.
- **Backfill import** (`app/importer.py`) — brain-dump (one item/line, via the router) or CSV; dedupe-aware, never prompts. `cli.py import <file|->`.
- **Pipeline stats** (`app/stats.py`, STATS intent) — funnel, response/interview/offer/ghost rates, stale count. "how am I doing".
- **Dates & deadlines** (`app/deadlines.py`, DEADLINE intent) — "stripe oa due friday" / "what's coming up"; setting one schedules a day-ahead reminder through the existing pipeline (auto-flips to SMS post-A2P). UPDATE with a date for a scheduled stage ("google onsite next tuesday") also lands on the calendar.
- **SMS menu** (`engine.MENU`) — grouped, example-driven command menu shown on greeting ("hi"/"hey", with a welcome line) and on help ("help"/"menu"/"?"/"get started"). `conversation.is_help` widened; a lone "?" triggers it but a question merely ending in "?" does not.
- **Web dashboard** (`app/dashboard.py`, `GET /`) — read-only HTML: stat cards, a visual **funnel bar**, follow-up priorities (🤝 marks recruiter signal), upcoming deadlines, **pending reminders**, a **searchable Applications list with expandable per-app event history** (`<details>` + tiny vanilla-JS filter, no framework), and recruiters. `?user=` switches user; `default_user()` = busiest.
- SQLite persistence; every state change writes an event row with the raw SMS.
- Local CLI REPL (`cli.py`) — same engine, no Twilio. Also `import` and `agenda` subcommands.

---

## 3. Waiting on (external / user action)

These are **not code gaps** — the app is ready; external approval or setup is blocking.

| Blocker | What's waiting | What to do when unblocked |
|---|---|---|
| **Stable hosting** | ngrok URL changes each session → Slack webhook needs re-verifying | **Scaffolding now in repo** (`Dockerfile`, `fly.toml`, `deploy/README.md`) — Docker build + volume persistence validated locally. Remaining = one user step: `fly launch --no-deploy` → `fly volumes create data` → `fly secrets set …` → `fly deploy`, then repoint Slack Events URL once. |
| ~~A2P 10DLC~~ (Twilio) | Superseded by Slack | Only relevant if you ever want SMS back: set `TWILIO_ACCOUNT_SID` + `TWILIO_FROM_NUMBER`, restart. Slack is the active path. |

**Small code TODO:** ~~Persist recomputed `next_follow_up_at`.~~ **Done** —
`store` now recomputes `next_follow_up_at` on every activity (create/update/note),
clears it for closed apps, and CHECK surfaces "⏰ Follow-up due …" when there's no
concrete deadline.

**Explicitly not built (by design):**
- Apollo enrichment (emails/LinkedIn via `people/match`) — costs export credits; we store `apollo_person_id` for future use but don't call enrichment.
- Auto-send outreach (LinkedIn/email) — draft is the product.

---

## 4. Architecture

```
Slack DM ──> POST /slack/events (FastAPI, app/main.py)   # Twilio /sms still wired, dormant
                  └─> slack.handle_event()               # app/slack.py (sig check, dedupe, loop guard)
                       └─> handle_sms(user_id, text)      # app/engine.py  (same brain for both transports)
                            ├─ router.parse_actions()     # app/router.py  (heuristic | Claude)
                            ├─ conversation.*              # app/conversation.py (pending exchange)
                            ├─ context.get/set()           # app/context.py (last company/role/app)
                            ├─ engine slot-filling + actions # ask / confirm / execute
                            ├─ outreach.discover_for_company() # app/outreach.py (cached Apollo)
                            └─ store.*                     # app/store.py -> SQLite (app/db.py)
                  <─ slack.post_message() (Web API)        # Twilio path replies with TwiML instead
```

| File | Role |
|---|---|
| `app/main.py` | FastAPI app, `GET /` (HTML dashboard), `/slack/events` (Slack webhook), `/sms` (TwiML, dormant), `/message` (JSON), `/health`. `init_db()` at import; scheduler on startup. |
| `app/slack.py` | **Slack transport.** `verify_signature`, `SlackSender` (outbound reminders), `post_message`, `handle_event` (inbound → `handle_sms` → reply). Loop guard + `event_id` dedupe. httpx only. |
| `app/engine.py` | The brain. `handle_sms`, multi-turn slot filling, confirmations, corrections, multi-action, `_do_*` actions, `MENU`, two-step destructive confirm (`_advance_delete`/`_advance_bulk`), `_bulk_matches`/`_age_to_days`. **Undo** (`_do_undo` + undo recording in each mutation). **Recent** (`_apply_window`/`_window_label`/`_do_recent`). **Multi-turn EDIT** (`_start_edit`/`_continue_edit`/`_apply_edit`). |
| `app/router.py` | Intent extraction. `HeuristicRouter` + `AnthropicRouter`, `parse_actions` → `list[ParsedMessage]`. |
| `app/conversation.py` | Pending-exchange state (SQLite) + yes/no/cancel/correction/greeting/help + `smalltalk_reply()` (thanks/ack/compliment/bye). 30-min expiry. |
| `app/context.py` | Per-user memory: `last_company`, `last_role`, `last_application_id`. |
| `app/store.py` | Application/event persistence: create/find/update/`add_note`, `edit_application` (corrections), `delete_application`, `list_events`, `has_recruiter_signal()`. `create_application` takes optional `applied_at` for backfill. **Undo**: `record_undo`/`get_undo`/`clear_undo`, `restore_application(fields)`, `delete_event`, `last_event_id`. **Recent**: `applications_in_window`. `next_follow_up_at` recomputed via `_next_followup` on each activity. |
| `app/db.py` | SQLite schema + idempotent migrations (`_migrate_schema`). |
| `app/intents.py` | `Intent` enum, `ParsedMessage`, canonical statuses. |
| `app/ratelimit.py` | Token-bucket rate limiter (LLM + Apollo). |
| `app/config.py` | pydantic-settings from `.env`. |
| `cli.py` | Local REPL + `import <file|->` (backfill) and `agenda` (deadlines) subcommands. |
| `app/scoring.py` | Follow-up prioritization → `_do_query`. |
| `app/reminders.py` | NL time parsing, persistence, `Sender`/`LogSender`/`TwilioSender`, `deliver_due_reminders`. `get_sender()` precedence: Slack → Twilio → Log. |
| `app/scheduler.py` | APScheduler poll loop + `python -m app.scheduler` one-shot tick. |
| `app/apollo.py` | **Only file that calls Apollo HTTP.** `discover_recruiters()`, limits, caching, `discovery_issue()`, `usage()`. |
| `app/outreach.py` | Recruiter persistence, `discover_for_company()` → `DiscoveryResult`, `apollo_footnote()`, `draft_outreach()`. |
| `app/importer.py` | Bulk backfill: `import_braindump()`, `import_csv()`, `ImportSummary`. Dedupe-aware, never prompts. |
| `app/stats.py` | Pipeline analytics: `compute_stats()` + `render()`. STATS intent. |
| `app/deadlines.py` | Dated events: `create_deadline()` (also schedules a reminder), `upcoming()`, `render_upcoming()`. DEADLINE intent. |
| `app/dashboard.py` | Read-only HTML render: `render(user_id)`, `default_user()`. Funnel bar, reminders, searchable apps + per-app history (`store.list_events`). Served at `GET /`. |
| `tests/` | 234 tests — adds `test_undo`, `test_recent` (relative-date LIST), `test_edit_flow` (multi-turn EDIT), `test_followup_persist` to the prior `test_slack`, `test_importer`, `test_stats`, `test_deadlines`, `test_dashboard`, `test_menu`, `test_assistant`, `test_corrections`, `test_bulk`. |

### Data model

- `applications`: id, user_id, company, role, status, applied_at, source, next_follow_up_at, last_updated_at.
- `application_events`: id, application_id, type, content, timestamp, raw_sms.
- `reminders`: id, user_id, application_id (nullable), remind_at, body, status, created_at, sent_at.
- `recruiters`: id, user_id, application_id (nullable), company, name, title, email, linkedin_url, source, **apollo_person_id**, created_at. Deduped on `(user_id, company, name)` or `apollo_person_id`.
- `deadlines`: id, user_id, application_id (nullable), company, label, due_at, status (open|done), created_at. Drives the upcoming/agenda view; creation also schedules a heads-up reminder.
- `undo_log`: user_id (PK — one row/user, overwritten each mutation), kind (apply|status|note|edit|bulk|delete), payload (JSON reversal data), summary, created_at. Backs single-level UNDO.
- `apollo_api_calls`: call log for daily caps + `/health` (people_search vs org_search).
- `company_domains`: cached org→domain lookups (when org search enabled).
- `company_domain_misses`: negative cache — don't re-spend org credits for 30 days.
- Canonical statuses: Applied, OA received, Phone screen, Interview, Onsite, Offer, Rejected, Ghosted.

### Apollo: credits, limits, caching

**What costs Apollo credits vs not:**

| Call | Endpoint | Credits? | Used by default? |
|---|---|---|---|
| People search | `mixed_people/api_search` | **No** | Yes — OUTREACH |
| Org → domain | `mixed_companies/search` | **Yes** | No — `APOLLO_ORG_LOOKUP_ENABLED=false` |
| Enrichment | `people/match`, `bulk_match` | **Yes** | Not implemented |

**Guardrails (`.env` / `.env.example`):**

| Setting | Default | Purpose |
|---|---|---|
| `APOLLO_MAX_RESULTS` | 3 | Contacts per discovery |
| `APOLLO_MAX_DISCOVERIES_PER_DAY` | 5 | New-company people searches / UTC day |
| `APOLLO_RATE_LIMIT_PER_MIN` | 3 | Burst cap; graceful skip |
| `APOLLO_ORG_LOOKUP_ENABLED` | false | Blocks credit-consuming org search |
| `APOLLO_MAX_ORG_SEARCHES_PER_DAY` | 3 | Org lookup cap when enabled |
| `APOLLO_ORG_MISS_CACHE_DAYS` | 30 | Skip repeat org credits after empty result |

**Don't waste API results:**
- **Company recruiter cache** — second OUTREACH for same company = SQLite only, no Apollo.
- **Domain piggybacking** — saves `organization.primary_domain` from free people search responses.
- **Org domain cache** + **negative miss cache** — when org lookup enabled.
- **`apollo_person_id` stored** — dedupe + future enrichment without re-search.
- **Sparse row merge** — re-discovery fills missing title/email/LinkedIn without dupes.

**SMS footnote** (bottom of OUTREACH reply via `outreach.apollo_footnote()`):
- Cached: `(Apollo: saved contacts — no API call.)`
- Fresh search: `(Apollo: people search — no credits used.)`
- Org lookup used: `(Apollo: people search (no credits) + N org lookup credit(s) (domain cached — won't repeat).)`

**`/health` Apollo block:** `discoveries_today`, caps, `people_searches`, `org_searches`, `org_lookup_enabled`, skip counters.

---

## 5. How to run it

```bash
# Python 3.12 or 3.13 (NOT 3.14 — pydantic-core had no wheels at setup time).
.venv/bin/pip install -r requirements.txt

# Local (same engine as SMS):
.venv/bin/python cli.py
.venv/bin/python cli.py "reach out to a recruiter at stripe"
.venv/bin/python cli.py import apps.csv      # bulk backfill (.csv → CSV, else brain-dump)
cat dump.txt | .venv/bin/python cli.py import -
.venv/bin/python cli.py agenda               # upcoming deadlines

# Web service:
.venv/bin/uvicorn app.main:app --reload --port 8000
#   GET  /             read-only HTML dashboard (?user= to switch user)
#   POST /slack/events Slack Events API webhook (primary transport)
#   POST /sms          Twilio webhook (From/Body form) -> TwiML (dormant)
#   POST /message      JSON: {"from": "...", "body": "..."}
#   GET  /health       router, LLM usage, reminder_scheduler, apollo, reminder_delivery

# Slack live (primary): set SLACK_BOT_TOKEN + SLACK_SIGNING_SECRET in .env, then:
ngrok http 8000
# -> Slack app → Event Subscriptions → Request URL https://<ngrok-host>/slack/events
# -> subscribe to bot events: message.im (+ app_mention). Server must be up first.

# Twilio (dormant fallback — inbound only, no outbound creds needed for replies):
# -> set Twilio number webhook to https://<ngrok-host>/sms

# Tests (offline — heuristic router, temp DB, no live API):
.venv/bin/python -m pytest -q

# Manual reminder tick (log-only until Twilio outbound):
.venv/bin/python -m app.scheduler
```

Config: `.env` (secrets, gitignored) + `.env.example` (template).

---

## 6. The two routers

Both implement `parse(text) -> ParsedMessage` and
`parse_actions(text) -> list[ParsedMessage]`. Factory: `get_router()` picks
Claude if `ANTHROPIC_API_KEY` is set, else heuristic.

`ParsedMessage` fields: `intent`, `company`, `role`, `status`, `message`,
`time_reference`, `confidence` (0–1).

### Heuristic router
Regex/keyword extraction. Never splits combined messages. **What CI exercises.**

### Claude router (`AnthropicRouter`)
- **Model: `claude-haiku-4-5`** — do not upgrade or reintroduce OpenAI without user asking.
- Structured outputs (JSON schema), token bucket (`LLM_RATE_LIMIT_PER_MIN=30`), graceful fallback to heuristic.
- Usage on `/health`. Few-shots for typos, multi-word companies, combined messages, and every intent incl. CHECK/EDIT/BULK (BULK convention: status=new stage, message=current-stage filter, time_reference=age).

---

## 7. Conversational behavior (engine)

Not one-shot per message. Slot filling, confidence repair (>0.8 execute,
0.4–0.8 infer/ask, <0.4 lean on context), confirmations, corrections, duplicate
guard, context switching, 30-min pending expiry. Multi-action up to 4 intents
(LLM-only).

**Assistant layer:**
- **Smalltalk** (`conversation.smalltalk_reply`): thanks/ack/compliment/sign-off →
  warm replies. Only fires when there's no pending exchange AND intent is UNKNOWN,
  so a mid-confirm "ok" still reads as yes (the confirm flow runs first).
- **CHECK** (`_do_check`): read-only per-app summary — stage, last-activity age,
  latest note, next deadline. Never mutates.
- **EDIT** (`_do_edit`): correct a saved entry's role/name/applied-date in place
  (writes an 'edit' event). Non-destructive → applies directly. "change X to
  <stage>" is redirected to UPDATE so stage changes stay in one place.
- **DELETE** + **BULK**: the only destructive paths, both **two-step** — they ask
  a confirm stating the consequence and act only on an explicit yes. BULK
  (`_advance_bulk`) previews the exact count + sample companies, filters by
  current stage and/or staleness age, and never sweeps terminal apps.

OUTREACH flow: `discover_for_company()` → top recruiter + `draft_outreach()` →
reply with contacts, draft, copy/paste note, Apollo footnote.

---

## 8. Gotchas / hard-won lessons

- **Empty env var shadows `.env`.** Fixed for `ANTHROPIC_API_KEY` in
  `get_settings()`. Same pattern can bite any env var — if something looks
  unconfigured, check `export VAR=""` in the shell.
- **Test isolation.** `conftest.py` forces `ANTHROPIC_API_KEY=""` and
  `APOLLO_API_KEY=""`, neutralizes dotenv fallback, resets router/Apollo/
  sender singletons. **Tests must never hit live APIs.**
- **Apollo plan vs API access.** Basic **trial** UI may show People access while
  API still returns 403 "free plan". Needs **paid Basic** + **master API key**
  created *after* conversion. `discovery_issue()` explains this in SMS.
- **Apollo `api_search` shape.** No `name` field — returns `first_name` +
  `last_name_obfuscated`. `_display_name()` builds display names; without it
  everyone deduped as "Unknown".
- **Apollo credits.** People search is free; org search and enrichment cost
  credits. Org lookup off by default. Don't add enrichment without explicit user
  request + credit budget.
- **Secrets hygiene.** Real keys in `.env` only. Keys have appeared in chat logs
  before — rotate if concerned. Never commit `.env`.
- **Python 3.14 doesn't work.** Use 3.12/3.13.
- **`init_db()` at import** in `app/main.py`; migrations in `_migrate_schema()`.
- **`Intent` is a `str` Enum** — string lookups from SQLite work.

---

## 9. Immediate next steps (priority order)

### 1. Slack live (DONE — now the primary transport)
Tokens set in `.env`, webhook verified, smoke-tested end-to-end. Running it:
1. `uvicorn app.main:app --port 8000` + `ngrok http 8000`
2. Slack app → Event Subscriptions → Request URL `https://<ngrok-host>/slack/events`,
   subscribe to `message.im` (+ `app_mention`). (Re-verify after each ngrok restart.)
3. DM the bot: `applied notion swe ii`, `what should I follow up on`,
   `reach out to a recruiter at stripe`.

**Already validated:** live user testing + automated smoke test through
`/slack/events` (challenge, inbound reply, loop guard, dedupe, persistence).

### 2. Stable hosting (scaffolding ready — Fly.io)
`Dockerfile` + `fly.toml` + `deploy/README.md` are in the repo (Fly.io target:
python:3.13-slim, persistent SQLite volume at `/data`, one always-warm machine so
the in-process scheduler keeps firing). Build + volume-persistence smoke-tested
locally with Docker. **To go live (user step):**
```bash
fly launch --no-deploy
fly volumes create data --size 1 --region iad
fly secrets set ANTHROPIC_API_KEY=… SLACK_BOT_TOKEN=… SLACK_SIGNING_SECRET=… APOLLO_API_KEY=…
fly deploy
```
Then repoint Slack Event Subscriptions Request URL to
`https://<app>.fly.dev/slack/events` once (stable forever after).

### 3. Outbound reminder delivery
Slack reminders are live via `SlackSender` (`get_sender()` precedence: Slack →
Twilio → Log). Test a tick: `python -m app.scheduler`. Optionally persist
`next_follow_up_at` from the scorer. Twilio SMS remains a dormant fallback,
still gated on A2P 10DLC if ever wanted.

### 4. Optional later
- Apollo enrichment (costs credits) — only if user wants emails/LinkedIn in-app
- Auto-send outreach with confirm step
- ~~Undo, relative-date queries, multi-turn EDIT~~ — **done this session.**
- ~~Sync `README.md`~~ — **done** (now reflects Slack-primary + full intent set).
- Wider conversational coverage still open: multi-level undo (currently
  single-level), undo of a delete (would need a soft-delete/recycle bin),
  relative-date queries over *events* not just `applied_at` ("what did I hear
  back on this week").

---

## 10. Build/preference notes

- **Anthropic only.** Haiku 4.5 for cost. Don't change without user asking.
- **Smart conversation before plumbing** — router/engine tuning > integrations unless told otherwise.
- **Token efficiency** for LLM — structured outputs, rate limits, tight prompts.
- **Keep heuristic path working** — free fallback + test suite backbone.
- **Never block the user** — graceful fallbacks everywhere (router, Apollo, drafting).
