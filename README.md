# Job Search Intelligence

A personal, conversational job-application tracker. You message it naturally
("applied spotify swe ii", "spotify oa received", "what should I follow up on")
and it logs applications, updates statuses, takes notes, tracks deadlines,
prioritizes follow-ups, finds recruiters, and holds a real multi-turn
conversation. Built as a high-speed personal execution engine for maximizing
interviews — not a SaaS product.

The brain (`engine.handle_sms`) is transport-agnostic. **Slack is the primary
channel** (Events API inbound + Web API outbound); a Twilio SMS front-end is kept
intact but dormant. The same engine also runs in a local CLI and behind a JSON
endpoint.

> For the full engineering status, design philosophy, and gotchas, see
> [`handoff.md`](handoff.md).

## Quick start (no API keys required)

```bash
# Use Python 3.12 or 3.13 (not 3.14 — pydantic-core has no wheels yet)
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # optional; defaults work out of the box

# Talk to it locally, exactly like the messaging channel:
.venv/bin/python cli.py
# or one-shot:
.venv/bin/python cli.py "applied spotify swe ii"
.venv/bin/python cli.py import apps.csv   # bulk backfill (.csv → CSV, else brain-dump)
.venv/bin/python cli.py agenda            # upcoming deadlines
```

With no `ANTHROPIC_API_KEY`, the system uses a built-in **heuristic router** that
runs fully offline. Set the key to switch to the Claude router automatically — no
code change.

## What you can text

| You text | It does |
|---|---|
| `applied spotify swe ii` | Logs Spotify — SWE II, status Applied |
| `spotify oa received` | Moves Spotify → OA received |
| `note spotify recruiter seemed positive` | Attaches a note |
| `list` / `list interview` | Lists all / a stage |
| `what did I apply to this week` | Recent applications in a time window |
| `what's the status of stripe` | Per-app summary (stage, last note, next deadline / follow-up) |
| `what should I follow up on` | Top follow-up priorities |
| `how am I doing` | Pipeline stats (funnel + response/interview/offer/ghost rates) |
| `stripe oa due friday` | Sets a deadline (and a heads-up reminder) |
| `what's coming up` | Your calendar of upcoming deadlines |
| `remind me about google in 3 days` | Schedules a reminder |
| `reach out to a recruiter at stripe` | Finds contacts (Apollo) + drafts an intro |
| `I'm looking for new grad SWE roles, remote or NYC` | Sets your match profile |
| `track openings at stripe` | Watches a company's job board; alerts on new fits |
| `what am I tracking` / `stop tracking stripe` | Manage tracked boards |
| `any new jobs` | Browse the latest discovered matches |
| `change the stripe role to SWE II` | Corrects a saved entry |
| `reject everything still in Applied` | Bulk stage change (confirms first) |
| `delete stripe` | Removes an application (confirms first) |
| `undo` | Reverses your last change |
| `hi` / `help` | Greeting / command menu |

### Intents

`APPLY, UPDATE, NOTE, LIST, QUERY, STATS, DEADLINE, CHECK, DELETE, EDIT, BULK,
UNDO, REMIND, OUTREACH, TRACK, JOBS, PROFILE, APPLY_JOB, DISMISS_JOB, SNOOZE_JOB,
TUNE, UNKNOWN` — all wired through both routers.

- **EDIT** is multi-turn: `change databricks` → *"What should I change about
  Databricks — its role, name, or applied date?"* → `role to SWE II`.
- **LIST** accepts a time window: `what did I apply to this week`,
  `anything new since monday`, `what did I apply to last month`.
- **UNDO** is single-level: it reverses the most recent reversible action
  (apply / status change / note / edit / bulk). A delete is *not* reversible and
  `undo` says so plainly rather than silently undoing the action before it.
- **DELETE** and **BULK** are the only destructive paths — both are two-step,
  showing the consequence and acting only on an explicit "yes".

### It actually holds a conversation

Messages aren't parsed in isolation — the bot remembers what it just asked and
threads your reply back. This works with either router backend.

- **Slot filling across turns** — missing pieces collected one question at a time.
- **Confirmations** — "yes / yeah / send it" and "no / nope / cancel".
- **Duplicate guard** — re-logging an existing company prompts instead of duping.
- **Corrections** — "no, google" mid-question replaces the slot and continues.
- **Smalltalk** — thanks / acks / sign-offs get warm replies, not the fallback.
- **Context switching** — a confident new command interrupts a half-finished one.

Stale exchanges expire after 30 minutes. The router attaches a confidence score
and the engine picks a mode (> 0.8 execute, 0.4–0.8 infer/ask, < 0.4 lean on
context) — it never discards input.

### Combined messages (multi-action)

One SMS can contain several requests — the Claude router returns a list of
actions and the engine runs each in order (capped at 4). Earlier actions update
context so later ones resolve against them. The offline heuristic router doesn't
split (multi-action is LLM-only).

## Claude (Anthropic) router

Set `ANTHROPIC_API_KEY` in `.env` to route intent parsing through Claude. It
defaults to **Claude Haiku 4.5** (`claude-haiku-4-5`), the cheapest capable model
for this classification/extraction task. Built for low token spend:

- **Structured outputs** (`output_config` JSON schema) guarantee valid JSON.
- **Tight packaging** — static instructions + few-shots live in one cached
  `system` block; only the SMS varies per request. `max_tokens` capped (512) and
  the inbound SMS truncated to `LLM_MAX_SMS_CHARS` (default 480).
- **Prompt caching** — `cache_control` on the system block (engages once the
  prompt grows past Haiku's cache minimum; harmless no-op below it).
- **Rate limiting** — a token bucket caps paid calls at `LLM_RATE_LIMIT_PER_MIN`
  (default 30/min). Over-limit calls and any API/network/auth error **fall back
  to the offline heuristic router** rather than failing.
- **Usage visibility** — `GET /health` reports cumulative tokens, calls, and
  fallbacks.

## Run the web service

```bash
.venv/bin/uvicorn app.main:app --reload --port 8000
```

| Endpoint | Purpose |
|---|---|
| `GET /` | Read-only HTML dashboard (`?user=` to switch user) |
| `POST /slack/events` | Slack Events API webhook (primary transport) |
| `POST /sms` | Twilio inbound webhook → TwiML (dormant) |
| `POST /message` | JSON convenience: `{"from": "...", "body": "..."}` |
| `GET /health` | Active router, LLM usage, scheduler, Apollo, reminder delivery |

The **dashboard** shows stat cards, a funnel bar, follow-up priorities (🤝 marks
recruiter signal), upcoming deadlines, pending reminders, a searchable
application list with expandable per-app history, and discovered recruiters.

### Point Slack at it (primary)

1. Set `SLACK_BOT_TOKEN` (xoxb-) and `SLACK_SIGNING_SECRET` in `.env`.
2. Expose locally: `ngrok http 8000` (server must be up first).
3. Slack app → **Event Subscriptions** → Request URL
   `https://<ngrok-host>/slack/events`; subscribe to bot events `message.im`
   (+ `app_mention`). Re-verify after each ngrok restart.
4. DM the bot. For a stable URL that doesn't need re-verifying, deploy it
   (see [`deploy/README.md`](deploy/README.md)).

### Point Twilio at it (dormant fallback)

Set your number's inbound webhook to `https://<ngrok-host>/sms` (HTTP POST).
Inbound replies work via TwiML with no outbound credentials. Outbound SMS is
gated on A2P 10DLC; Slack is the active path.

## Reminders & scheduling

A reminder is just a row with a due time; an APScheduler poll loop delivers due
ones through a `Sender`. `get_sender()` precedence is **Slack → Twilio → Log**,
so once `SLACK_BOT_TOKEN` is set, reminders deliver over Slack. Deadlines also
schedule a day-ahead heads-up through the same pipeline.

```bash
.venv/bin/python -m app.scheduler   # one-shot tick (manual)
```

In-process scheduling means the server must stay up for reminders to fire —
relevant when deploying (keep one instance warm; see the deploy notes).

## Recruiter discovery (Apollo)

`reach out to a recruiter at <company>` runs a free Apollo people search,
persists/dedupes contacts, and drafts an intro with Claude. **No auto-send** —
the draft is the product, you copy/paste. Credit guardrails (daily caps, caching,
org-lookup off by default) live in `app/apollo.py`; see `handoff.md` §4.

## Job discovery (Phase 1)

Beyond tracking applications you log, the assistant can **find** jobs and alert you
when new ones drop — all on free, no-auth sources:

1. **Set a profile:** `I'm looking for new grad SWE roles, remote or NYC`.
2. **Track companies:** `track openings at stripe`. It auto-detects the company's
   public board across **Greenhouse / Lever / Ashby** (no API key, no cost).
3. **Get alerted:** a background loop (`app/discovery.py`, every `JOB_POLL_SECONDS`)
   polls tracked boards, dedupes against everything already seen, runs a free
   keyword/location pre-filter, scores survivors 0–1 (Claude Haiku when a key is
   set, else a free heuristic), and Slack-DMs you the ones above
   `JOB_RELEVANCE_THRESHOLD` — reusing the same sender as reminders. Each alert
   prints a `#<id>`.
4. **Browse anytime:** `any new jobs`.
5. **Assisted apply (Phase 2):** `apply 2` (or `apply to the stripe one`) hands
   back the apply link plus a drafted *"why I'm a fit"* blurb (Claude when keyed,
   else a template from your profile), logs the role as **Applied**, and marks the
   posting applied. It never auto-submits — you paste the draft yourself.
6. **Manage the feed:** `dismiss 2` hides a posting for good; `snooze 2 for a
   week` mutes it until it resurfaces; `only show 80%+ matches` / `be less picky`
   / `reset matching` tune your per-user alert threshold; `what am I tracking`
   shows per-board counts + the active threshold. The web dashboard (`GET /`) has
   a **Job discovery** section listing tracked boards and the latest matches.

A generic **RSS/Atom** source (`app/jobsources/rss.py`) is also registered for
feed-based boards (e.g. "Who is hiring" aggregations), alongside Greenhouse /
Lever / Ashby.

### Paid aggregator (Phase 3) — optional, off by default

`app/jobsources/aggregator.py` adds a **SerpApi-style Google Jobs search** that
runs against your *profile* (roles + locations), so discovery isn't limited to
the company boards you track — it surfaces matching roles from across the web,
which then flow through the same dedupe → pre-filter → score → alert pipeline (and
the same `apply <#>` assisted-apply path).

Because it costs money per call it's gated like the Apollo recruiter lookup:

- **Two switches:** it runs only when `AGGREGATOR_SEARCH_ENABLED=true` **and**
  `AGGREGATOR_API_KEY` is set (`Settings.aggregator_active`).
- **Budget caps:** a DB-backed daily search cap (`AGGREGATOR_MAX_CALLS_PER_DAY`,
  UTC day, survives restarts) + a per-minute rate limit; over budget → it skips.
- **No first-run storm:** the first aggregator pass for a user baselines current
  results silently (status `seeded`), so only roles appearing *after* you enable
  it get alerted.
- **Never blocks:** any no-key / over-budget / network / parse error returns `[]`.

When active, `/health` gains an `aggregator` block (searches, today's count vs
cap, skips, errors). LinkedIn remains deferred (Phase 4).

Cost controls: free sources first; the LLM only ever sees pre-filtered postings,
batched into one call, capped at `JOB_MAX_SCORED_PER_TICK` per tick; each posting
is scored and alerted exactly once (dedup on `(user, source, external_id)`). The
paid aggregator adds its own per-day budget on top.

Run a one-shot pass manually: `.venv/bin/python -m app.discovery`. Discovery health
is on `/health` under `discovery`.

## Architecture

```
Slack DM ──> POST /slack/events (FastAPI)        # Twilio /sms wired, dormant
                  └─> slack.handle_event()        # sig check, dedupe, loop guard
                       └─> handle_sms(user, text)  # same brain for both transports
                            ├─ router.parse_actions()   # heuristic | Claude
                            ├─ conversation.*            # pending exchange
                            ├─ context.get/set()         # last company/role/app
                            ├─ engine slot-filling       # ask / confirm / execute
                            ├─ outreach.discover_for_company()  # cached Apollo
                            └─ store.*                   # SQLite
                  <─ slack.post_message() (Web API)
```

| File | Role |
|---|---|
| `app/main.py` | FastAPI app; dashboard, Slack/Twilio/JSON webhooks, `/health`; scheduler on startup. |
| `app/slack.py` | Slack transport: signature verify, `SlackSender`, `post_message`, `handle_event`. |
| `app/engine.py` | The brain: slot filling, confirmations, corrections, multi-action, undo, all `_do_*` actions. |
| `app/router.py` | Intent extraction: `HeuristicRouter` + `AnthropicRouter` (Claude Haiku 4.5). |
| `app/conversation.py` | Pending-exchange state + yes/no/cancel/correction/greeting/help/smalltalk. |
| `app/context.py` | Per-user memory (last company/role/app). |
| `app/store.py` | Application/event persistence, edits, deletes, undo log. |
| `app/db.py` | SQLite schema + idempotent migrations. |
| `app/intents.py` | `Intent` enum, `ParsedMessage`, canonical statuses. |
| `app/scoring.py` | Follow-up prioritization. |
| `app/reminders.py` | NL time parsing, persistence, senders, delivery. |
| `app/scheduler.py` | APScheduler poll loop. |
| `app/apollo.py` | The only file that calls Apollo HTTP. |
| `app/outreach.py` | Recruiter persistence + draft generation. |
| `app/importer.py` | Bulk backfill (brain-dump or CSV). |
| `app/stats.py` | Pipeline analytics. |
| `app/deadlines.py` | Dated events + agenda. |
| `app/dashboard.py` | Read-only HTML dashboard. |
| `cli.py` | Local REPL + `import` / `agenda` subcommands. |

## Data model

SQLite (`app/db.py`). Core tables: `applications`, `application_events` (every
state change writes an event row with the raw message, so nothing is lost),
`context_memory`, `conversation_state`, `reminders`, `recruiters`, `deadlines`,
`undo_log`, plus Apollo bookkeeping (`apollo_api_calls`, `company_domains`,
`company_domain_misses`). `next_follow_up_at` is kept live as activity changes and
cleared for closed applications.

Canonical statuses: Applied, OA received, Phone screen, Interview, Onsite, Offer,
Rejected, Ghosted.

## Tests

```bash
.venv/bin/python -m pytest -q     # 234 tests, fully offline (~3s)
```

`conftest.py` forces the offline heuristic router, neutralizes live API keys
(Anthropic/Apollo/Slack), and gives each test a throwaway SQLite file — **tests
never hit live APIs.**

## Deployment

See [`deploy/README.md`](deploy/README.md) for a Fly.io runbook (Dockerfile +
persistent SQLite volume + a warm instance for the scheduler).
