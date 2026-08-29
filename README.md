# Job Search Intelligence

A personal, conversational job-application engine. **Invite-only iOS beta:** Sign in
with Apple, discover and rank roles, prepare answers and a tailored resume, and
⚡ Autofill Greenhouse / Lever / Ashby forms in the in-app browser — **you always
click Submit**. Built as a high-speed personal execution engine.

The brain (`engine.handle_sms`) is transport-agnostic. **In-app chat** (iOS Chat
tab) is the product channel. The same engine also runs in a local CLI.

> For engineering status, see [`handoff.md`](handoff.md).
> For TestFlight + allowlist, see [`deploy/BETA.md`](deploy/BETA.md).

## Quick start (no API keys required)

```bash
# Use Python 3.12 or 3.13 (not 3.14 — pydantic-core has no wheels yet)
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # optional; defaults work out of the box

# Talk to it locally — same commands as in-app chat:
.venv/bin/python cli.py
# or one-shot:
.venv/bin/python cli.py "applied spotify swe ii"
.venv/bin/python cli.py import apps.csv   # bulk backfill (.csv → CSV, else brain-dump)
.venv/bin/python cli.py agenda            # upcoming deadlines
```

With no `ANTHROPIC_API_KEY`, the system uses a built-in **heuristic router** that
runs fully offline. Set the key to switch to the Claude router automatically — no
code change.

## What you can say (chat commands)

| You say | It does |
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
UNDO, REMIND, TRACK, JOBS, PROFILE, APPLY_JOB, DISMISS_JOB, SNOOZE_JOB,
TUNE, REMEMBER, UNKNOWN` — all wired through both routers.

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

One message can contain several requests — the Claude router returns a list of
actions and the engine runs each in order (capped at 4). Earlier actions update
context so later ones resolve against them. The offline heuristic router doesn't
split (multi-action is LLM-only).

## Claude (Anthropic) router

Set `ANTHROPIC_API_KEY` in `.env` to route intent parsing through Claude. It
defaults to **Claude Haiku 4.5** (`claude-haiku-4-5`), the cheapest capable model
for this classification/extraction task. Built for low token spend:

- **Structured outputs** (`output_config` JSON schema) guarantee valid JSON.
- **Tight packaging** — static instructions + few-shots live in one cached
  `system` block; only the inbound text varies per request. `max_tokens` capped (512) and
  the inbound message truncated to `LLM_MAX_SMS_CHARS` (default 480).
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
| `POST /chat` | In-app assistant (Bearer session) |
| `POST /auth/apple` | Exchange an Apple identity token for a session |
| `POST /feedback` | Tester feedback (session) |
| `GET /apply/setup` | First-run wizard status |
| `GET /apply/data` | Staged matches + identity (session) |
| `GET /apply/rules` | Autofill rules for iOS WebView |
| `GET /apply/resume` | Tailored resume PDF |
| `GET /apply/cover` | Optional one-page cover letter PDF |
| `GET /health` | Router, LLM usage, scheduler, auth flags, discovery |

## Reminders & scheduling

A reminder is just a row with a due time; an APScheduler poll loop delivers due
ones through a `Sender`. **`AppSender`** (default) appends to the in-app chat
transcript and sends a best-effort APNs push. Deadlines also schedule a day-ahead
heads-up through the same pipeline.

```bash
.venv/bin/python -m app.scheduler   # one-shot tick (manual)
```

In-process scheduling means the server must stay up for reminders to fire —
relevant when deploying (keep one instance warm; see the deploy notes).


## Job discovery (Phase 1)

Beyond tracking applications you log, the assistant can **find** jobs and alert you
when new ones drop:

1. **Set a profile:** `I'm looking for new grad SWE roles, remote or NYC`. This turns on
   **wide discovery** — you do **not** need a list of companies:
   - **RSS feeds** (HN Who's Hiring, Remote OK, Himalayas, Remotive)
   - **ATS directory** — rotates through public Greenhouse / Lever / Ashby / Workable / SmartRecruiters boards
   - **Simplify intern + new-grad lists**, plus **Y Combinator** jobs
2. **Optional:** `track openings at stripe` for a specific company's board, or
   `track feed hn-hiring` for an extra RSS feed.
3. **Get alerted:** a background loop (`app/discovery.py`, every `JOB_POLL_SECONDS`)
   polls tracked boards, dedupes against everything already seen, runs a free
   keyword/location pre-filter, scores survivors 0–1 (Claude Haiku when a key is
   set, else a free heuristic), and queues matches above `JOB_RELEVANCE_THRESHOLD`
   (per-user tunable). By default (`JOB_ALERT_MODE=digest`) you get **one summary
   message per poll** in chat (plus optional push), not one message per job — set
   `instant` for per-job pings or `silent` to store only.
4. **Browse / review:** `any new jobs` (quick list) or **`review jobs`** to walk
   the queue one-by-one (skip / apply / stop · `dismiss all` clears it).
5. **Assisted apply:** `apply 2` (or `apply to the stripe one`) hands back the
   apply link, a drafted *"why I'm a fit"* blurb, and a **one-page tailored resume
   PDF**. Claude edits your base `.tex`, Tectonic compiles it, trims to one page if
   needed, and caches the result for reuse. An optional **cover letter** is built
   only when you ask (iOS documents menu / `GET /apply/cover`) — same one-page
   rule, business-letter layout we own. Never auto-submits — you paste the draft
   and attach the resume (and cover letter if needed) yourself.
6. **Manage the feed:** `dismiss 2` hides a posting for good; `snooze 2 for a week`
   mutes it until it resurfaces; `only show 80%+ matches` / `be less picky` /
   `reset matching` tune your per-user threshold; `what am I tracking` shows
   per-board counts + the active threshold. Chat and `/health` surface
   **Job discovery** section listing tracked boards and the latest matches.

### Wide discovery — find jobs without naming companies

Once a profile is set, each tick also scans (all merging into the same
dedupe → pre-filter → score → queue → review pipeline):

- **RSS feeds** (`JOB_WIDE_RSS_FEEDS`, e.g. HN "Who's hiring", Remote OK, Himalayas, Remotive, We Work Remotely). Extra WWR categories (design, product, sales, support, finance, devops) are selected from the job-search profile. On by default.
- **An ATS directory** (`data/ats_boards.json`, Greenhouse / Lever / Ashby / Workable / SmartRecruiters), rotating a batch per tick (`JOB_DIRECTORY_BOARDS_PER_TICK`), **filtered to the profile's field**. Apply URLs from other feeds teach the directory new board slugs. A larger employer catalog (`data/company_catalog.json`) stores 1,000+ company *names* per major field as references — hospitals, universities, listed companies — but only boards with a public ATS API are polled.

- **Pitt CSC / Simplify lists** (`JOB_SWELIST_LIST`, internships + new-grad) — on by default.
- **Y Combinator** public jobs page — on by default.

Cost controls: free sources first; the LLM only ever sees pre-filtered postings,
batched into one call, capped at `JOB_MAX_SCORED_PER_TICK` per tick; each posting
is scored and alerted exactly once (dedup on `(user, source, external_id)`). The

Run a one-shot pass manually: `.venv/bin/python -m app.discovery`. Discovery health
is on `/health` under `discovery`.

## Resume tailoring

Two base resumes (`swe.tex`, `aiml.tex`) live in `resumes/` locally and on the Fly
volume in production — **not in git** (personal info). See [`resumes/README.md`](resumes/README.md).

| Step | What happens |
|---|---|
| `apply <#>` | Picks SWE vs AI/ML base → Claude edits body only → lock reference layout → Tectonic compile → trim whole extra bullets to 1 page (never a clipped or 2-page file) |
| Cache hit | Reuses a PDF only for the same posting, or the same company + title + JD. A nearby title is tailored again. |
| iOS | PDF via `GET /apply/resume` or the apply documents menu — attach manually in WebView. Cover letter is the same menu, built when you ask (`GET /apply/cover`). |

**Fly setup:** copy base `.tex` to `/data/resumes/` on the volume (see
[`deploy/README.md`](deploy/README.md)). The Docker image includes Tectonic.

## Architecture

```
iOS Chat tab ──> POST /auth/apple + POST /chat (FastAPI)
Apply tab    ──> GET /apply/* + GET /apply/rules (WKWebView autofill)
                      └─> handle_sms(user, text)  # same brain for CLI/chat
                           ├─ router.parse_actions()   # heuristic | Claude
                           └─ store.*                   # SQLite
```

| File | Role |
|---|---|
| `app/main.py` | FastAPI app; chat + apply JSON APIs, `/health`; scheduler on startup. |
| `app/auth.py` | Sign in with Apple, session tokens, invite allowlist. |
| `app/chat.py` | Chat transcript + send path (reminders/digests land here too). |
| `app/engine.py` | The brain: slot filling, confirmations, corrections, multi-action, undo, all `_do_*` actions. |
| `app/router.py` | Intent extraction: `HeuristicRouter` + `AnthropicRouter` (Claude Haiku 4.5). |
| `app/conversation.py` | Pending-exchange state + yes/no/cancel/correction/greeting/help/smalltalk. |
| `app/context.py` | Per-user memory (last company/role/app). |
| `app/store.py` | Application/event persistence, edits, deletes, undo log. |
| `app/db.py` | SQLite schema + idempotent migrations. |
| `app/intents.py` | `Intent` enum, `ParsedMessage`, canonical statuses. |
| `app/scoring.py` | Follow-up prioritization. |
| `app/reminders.py` | NL time parsing, persistence, `AppSender`, delivery. |
| `app/scheduler.py` | APScheduler poll loop. |
| `app/push.py` | APNs delivery for new matches and reminders. |
| `app/discovery.py` | Background job polling + alert delivery. |
| `app/apply_queue.py` | Stage postings, assemble packages (answers + resume). |
| `app/fieldmatch.py` | Shared autofill rules (`GET /apply/rules`). |
| `app/formprobe.py` | Form-page detection heuristics (login wall, captcha, submit). |
| `app/outreach.py` | Recruiter persistence + draft generation. |
| `app/resume_tailor.py` | Resume pick/edit/compile/trim for assisted apply. |
| `app/coverletter.py` | Optional one-page cover letter (on demand, not Preflight). |
| `app/resume_store.py` | Tailored resume + cover letter cache (volume + SQLite). |
| `app/importer.py` | Bulk backfill (brain-dump or CSV). |
| `app/stats.py` | Pipeline analytics. |
| `app/deadlines.py` | Dated events + agenda. |
| `ios/` | SwiftUI app: Apply · About · Chat · Settings. |
| `cli.py` | Local REPL + `import` / `agenda` subcommands. |

## Data model

SQLite (`app/db.py`). Core tables: `applications`, `application_events` (every
state change writes an event row with the raw message, so nothing is lost),
`context_memory`, `conversation_state`, `reminders`, `chat_messages`, `recruiters`,
`deadlines`, `undo_log`, plus legacy recruiter bookkeeping (`apollo_api_calls`, `company_domains`,
`company_domain_misses`). `next_follow_up_at` is kept live as activity changes and
cleared for closed applications.

Canonical statuses: Applied, OA received, Phone screen, Interview, Onsite, Offer,
Rejected, Ghosted.

## Tests

```bash
.venv/bin/python -m pytest -q     # full pytest suite (~700 tests, fully offline)
```

`conftest.py` forces the offline heuristic router, neutralizes live API keys
(Anthropic), and gives each test a throwaway SQLite file — **tests never
hit live APIs.**

## Deployment

See [`deploy/README.md`](deploy/README.md) for a Fly.io runbook (Dockerfile +
persistent SQLite volume + a warm instance for the scheduler). TestFlight beta:
[`deploy/BETA.md`](deploy/BETA.md).
