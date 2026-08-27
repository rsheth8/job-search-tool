# Job Search Intelligence — Engineering Handoff

> Pick-up doc for a fresh session. Last updated **2026-08-27**.
> Invite-only iOS beta: Sign in with Apple, session-scoped data, first-run setup,
> TestFlight. See [`deploy/BETA.md`](deploy/BETA.md). Slack is rollback-only.
> Live ATS is still a headful human pass (`worker/LIVE_TEST.md`).
>
> `worker-robust-fill` is merged to `main` (PR #30). Auto-submit stays **off**
> for testers (`APPLY_AUTOSUBMIT_ENABLED=false`).

---

## 0. TL;DR — where we are right now

A **personal, conversational job-search engine** with an **invite-only iOS
beta**. Testers sign in with Apple, finish a setup wizard (roles → identity →
one project), then discover → prepare → ⚡ Autofill in the in-app browser.
In-app chat is the assistant; Slack is disabled unless
`SLACK_TRANSPORT_ENABLED=true`.

**Isolation:** personal JSON APIs prefer the Bearer session and ignore `?user=`.
Production sets `AUTH_FAIL_OPEN=false` so a blank `APPLY_API_TOKEN` is not a
hole. `AUTH_ALLOWED_EMAILS` is the invite list.

**The one not-fully-proven piece:** live ATS DOMs for the Playwright worker.
Testers do **not** use auto-submit; they Autofill and submit themselves.

---

## 0. TL;DR — where we are right now

A **personal, conversational job-search engine**: you chat with it on Slack; it
discovers jobs, ranks them with a model trained on *your* taste, prepares complete
applications (tailored answers + resume), and — with a browser extension on desktop
or a headless worker driven from your phone — **fills and submits** them after you
approve. The **entire envisioned pipeline is built**. What remains is **operational
(deploy + live-test the submit worker)**, not new feature work.

**The full pipeline:** discover → rank (personalized) → auto-queue top matches →
prepare (per-question answers + tailored resume + identity) → review on phone →
auto-fill & submit (preview → you approve → it submits) → auto-track.

**The one not-fully-proven piece:** the Phase 2 submit **worker** drives a live
browser. Its fill logic is now covered by `tests/test_worker_fill.py` (real headless
Chromium against local fixtures), so what's left is confirming real ATS DOMs behave
like the fixtures — a headful live-test, not a rewrite.

**The whole loop now runs from Slack:** a filled application messages you what it
filled and what it left for you; reply `approve` or `cancel`. `in flight` shows
everything in progress. The `/apply` page still works unchanged.

---

## 1. Stack & deployment

- **App:** FastAPI + SQLite. Python 3.13. Entry: `uvicorn app.main:app`.
- **LLM:** Anthropic **Claude Haiku 4.5** only (`claude-haiku-4-5`). Cheap-but-capable.
  Every paid call is gated, rate-limited (token bucket), daily-capped, and
  **fail-open** to a free heuristic/template. CI never hits a network or real model.
- **Transport:** **In-app chat** is primary (`POST /chat`, Bearer session). Slack
is rollback (`SLACK_TRANSPORT_ENABLED`). Twilio SMS is dormant. Engine is
transport-agnostic.
- **Hosting:** **Fly.io**, app `job-search-tool`, region `iad`, URL
  `https://job-search-tool.fly.dev`. One always-warm 512MB machine (so the in-process
  APScheduler reminder + discovery loops keep ticking). SQLite on a **1GB persistent
  volume at `/data/job_search.db`** (`DATABASE_PATH` set in the Dockerfile).
- **Repo:** GitHub `rsheth8/job-search-tool` (private). `gh` account `rsheth8`,
  Fly account `rahilsheth05@gmail.com`. Redeploy: push to `main` (CI pytest must
  pass first — `.github/workflows/pytest.yml`, then `fly-deploy.yml`) or
  `flyctl deploy -a job-search-tool` from the repo root. Migrations are idempotent
  (run at import). **Do not treat a green fixture suite as a live ATS pass.**
- **Local venv:** `/Users/rahilsheth/Documents/job-search-tool/.venv/bin/python`
  (the repo `.python-version` pins 3.13 which the user's pyenv lacks — **use the
  venv python directly**, not `python`).

---

## 2. The subsystems (what's built)

### A. Conversational CRM (the original core)
Two-stage router (fast heuristic regex → Claude fallback) + slot-filling engine.
Intents: track/apply/update/check/edit/bulk/delete/undo, reminders, deadlines,
stats, recent-window queries, multi-action, smalltalk. Two-step confirms on
destructive ops; single-level undo with tombstones. Read-only dashboard at `/`.
Files: `router.py`, `engine.py`, `intents.py`, `conversation.py`, `store.py`,
`scoring.py`, `reminders.py`, `deadlines.py`, `stats.py`, `importer.py`, `dashboard.py`.

### B. Job discovery
Background scheduler runs `discovery.tick` per user. Wide sourcing: free ATS
(Greenhouse/Lever/Ashby), RSS feeds, rotating ATS directory (~54 boards), paid
SerpApi aggregator (capped). Pipeline per posting:
`fetch → dedupe → quality(reputability) → ghost-job filter → eligibility gate →
prefilter+cap → score → re-rank → threshold → alert (digest/instant/silent)`.
Per-posting Slack actions: `apply N`, `queue N`, `dismiss N`, `snooze N for a week`,
`be less picky` (TUNE). Files: `discovery.py`, `wide_discovery.py`, `jobsources/`,
`jobstore.py`, `profile.py`, `matcher.py`, `quality.py`, `posting_match.py`,
`job_alerts.py`, `jobs_review.py`.

### C. Matching v2 (the ML brain) — `app/matcher.py`, `reranker.py`, `embeddings.py`, `insights.py`, `eligibility.py`, `jobsources/ghost.py`, `trainer.py`
- **Personalized re-ranker** (`reranker.py`): pure-Python L2 logistic regression on
  *your* apply/dismiss/swipe labels. Features: relevance, kw_overlap, title_hit,
  loc_match, is_remote, first_party + **LLM-as-feature** (`fit_score`/`tech_overlap`/
  `stretch` from the summarizer). `llm_fit` is the #1 signal (+0.06 AUC) — **but only
  when `posting_summaries` is populated for the labelled postings.** Those judgements
  live in a cache keyed by `source:external_id`, not by user, so they're easy to lose
  in a move; when they're absent every LLM feature falls back to the same neutral
  default, the three weights collapse to one near-zero value, and the model quietly
  reverts to its without-`llm_fit` baseline of AUC 0.730. If `scripts.analyze_reranker`
  shows `fit_score`/`tech_overlap`/`stretch` at identical small weights, that's the
  symptom — check the cache, not the model. Has a
  **hold-out promotion guard** (won't replace a model with a worse one) and
  **outcome-graded labels** (an applied job that reached onsite/offer counts as a
  stronger positive than a bare "Applied"; reads the furthest stage from event history).
- **Embeddings** (Voyage `voyage-3-lite`): optional semantic sort. Measured ~0 gain
  for personalization — **fine to leave off**.
- **Ghost filter + eligibility gate**: rules-based, free, on by default.
- **Swipe trainer** `/train`: 3 modes — Best, Mix, **Learn** (active learning:
  surfaces postings the model is least sure about). Pass-imbalance nudge.

### C2. Personal knowledge (`app/knowledge.py`)
Durable facts that make a drafted answer *yours*: projects, achievements,
strengths, preferences, and reusable answers. Two uses — `knowledge_block()`
grounds the drafting prompt, and a saved answer matching the question is returned
**verbatim with no model call**. Taught from Slack (`remember project: …`,
`remember answer to "Why us?": …`), inspected with `what do you know about me`,
which also reports the identity-coverage audit (`knowledge.audit`). No demographic
data, same as `applicant.py`.

### D. Application assistance
- **Apply queue** (`apply_queue.py`, page `/apply`): stage a posting (`queue N`,
  `queue top 3`, or auto-queue), and it assembles the full package: **per-question
  tailored answers** (one batched Haiku call, cached), **tailored resume**, and the
  **applicant identity** map. Mobile-first review page: confirm identity, edit/copy/
  redraft each answer, "Open & submit," or "🤖 Auto-fill & submit."
- **Applicant identity** (`applicant.py`): name/email/phone/links/location/work-auth/
  education/etc. as a JSON blob on the profile. **EEO/demographic fields excluded.**
  Also: `referral_source` (canonical synonym key — see `SOURCE_PATTERNS`) and
  `previously_employed` (bool → Yes/No). Store `referral_source` as one of
  `company career site` | `job board` | `linkedin` | `referral` | `recruiter` |
  `event` — never the company-specific option text.
- **Resume tailoring** (`resume_tailor.py`): per-posting tailored one-page PDF
  (SWE vs AI/ML variant), Tectonic-compiled, cached. Needs base `.tex` on the volume
  (see §5).

### D2. The iPhone app — `ios/` (SwiftUI + WKWebView)
Four tabs: **Apply** (matches + `Prepare application` to stage, each row showing
*why* it surfaced), **In flight** (the worker's previews + the approve/cancel gate),
**About me** (`knowledge` + the coverage audit), **Settings** (backend + push).
An in-app browser opens the real form; ⚡ Autofill fills it — including Greenhouse
react-select comboboxes and Ashby Yes/No button pairs (see §2.G). Push notifications
(APNs) fire on new matches and on a preview awaiting approval, deep-linking to the
right tab. See `ios/README.md`.

### E. The autofill extension — `extension/` (MV3, load unpacked)
A browser extension (Chrome/Edge/desktop; Safari-packageable for iOS). Focus a
field → inline chip with the value or a drafted answer. **⚡ Autofill this page**
button fills everything recognized at once (text, dropdowns, Yes/No radios,
react-select comboboxes). Options page edits identity (syncs to server). Talks to
`/apply/identity` + `/apply/answer`. Covered by `tests/test_extension_autofill.py`
(shipping `content.js` in real Chromium): identity, native select, Yes/No radios,
optional EEO, never-submit, bundled/malformed-rules fallback. Bulk fill does **not**
draft essays and does **not** click Ashby Yes/No `<button>` pairs (iOS does).

### F. Phase 2 submit worker — `worker/` + `app/fill_requests.py` + `app/fieldmatch.py`
The phone-first auto-submit. `app/fieldmatch.py` = the **one** field-matching brain
(label→identity key, select/Yes-No matching, EEO never-fill), now *served* to the
other two surfaces via `GET /apply/rules` rather than hand-ported into them — the
JS copies had drifted behind it, and the iOS one was filling demographic fields the
worker refuses. `tests/test_rules_parity.py` proves Python and JS agree label-for-
label in a real browser; `tests/test_ios_autofill.py` runs the iOS engine itself
against the worker's fixtures. `app/fill_requests.py` = state machine
`pending → filling → preview → approved → submitting → submitted | failed` (submit
only after explicit approval). `worker/` = separate **Playwright-Python** Fly app
(~2GB, scale-to-zero) that claims a request, opens the public form, fills via
`fieldmatch`, screenshots a preview to your phone, waits for approval, submits.

**Approval is conversational**: `/worker/preview` messages the user, and `approve` /
`cancel` / `in flight` are routed intents (`APPROVE_FILL`, `APPLY_STATUS`). Only an
explicit human approval moves a request to `approved` — tested, including approving
early or as a different user. The filler handles native selects, ARIA comboboxes,
and Yes/No radio groups, and logs one structured line per field.

### F2. The LLM browser agent — `worker/agent.py` (`WORKER_AGENT=true`)
The newest piece, and the one the rest of this doc predates. The hard-coded filler
loses to iframes, multi-step "Apply → Next → Next" flows, and label variety. The agent
is the **hybrid** answer: every step runs the free deterministic pass (`auto_fill`,
the `fieldmatch` rules) first, and the model is consulted **only when that pass stops
making progress** — clicking through steps, writing the free-text answers, picking
Yes/No radios. A clean one-page form costs ~1–2 calls, not one per field.

It perceives the page as a list of interactive elements tagged with stable ids
(across every frame) and takes one action per step. The safety model is unchanged
and enforced in code, not just the prompt: it **never submits** (it calls
`ready_for_review` and hands off to the same human preview → approve gate), **never
fills EEO/demographic fields** (`act()` refuses `fill`/`choose`/`click` on any label
`fieldmatch.is_eeo` matches, whatever the model asked for), and calls `blocked` on a
login wall or captcha so the job falls back to the desktop extension.

Model is `AGENT_MODEL` (default `claude-opus-4-8` — `claude-haiku-4-5` is ~5x cheaper
and the model choice dominates every other cost knob). Per-turn ceiling is
`AGENT_MAX_TOKENS` (adaptive thinking spends this too, so too low truncates a turn
before it acts and the run reports `incomplete`); `AGENT_KEEP_STEPS` bounds the
conversation window, without which token cost grows with the square of the step count.

**Per-form spend is capped by `AGENT_TOKEN_BUDGET`** (default 150k ≈ $0.50–1.00 on
Opus): the agent stops, keeps what it filled, and reports why. There is deliberately
**no daily cap** — the worker scales to zero with no volume and no DB, so an
in-process day counter would reset every job and cap nothing while appearing to. A
real one has to be server-side; `worker/README.md` sketches it. Every run logs
`[agent] done: status=… model=… tokens=… of … budget`. Covered by
`tests/test_agent.py` with fakes — no browser, no API call.

### G. Autofill hardening — 2026-07-27 (Affirm live-verified)

Shipped on **`worker-robust-fill`**, verified on the live Affirm Greenhouse form.
Phone + extension share the same vocabulary via `fieldmatch.rules_payload()`.
Current checkout collects **822** tests; the autofill Chromium slice is green
(`tests/test_worker_fill.py`, `test_ios_autofill.py`, `test_extension_autofill.py`,
`test_rules_parity.py`).

**Two new identity keys.** Label rules were the easy half; value-side matching was
the real work, because neither question answers by string comparison:

| Key | Stored value | How an option is picked |
|---|---|---|
| `referral_source` | one of `SOURCE_PATTERNS` keys (e.g. `"company career site"`) | synonym table → company's wording ("Affirm's Career Site", "Render careers page") |
| `previously_employed` | bool → `"Yes"`/`"No"` | `_yes_no_option` — negation, not substring |

`"no"` is a substring of "I have **no**t previously been employed" (right by luck)
and of "**No**rway" (wrong). `_yes_no_option` reads meaning and **refuses to guess**
when ambiguous: "Yes, as an intern" and "Yes, as a full-time employee" are both
affirmative → skip. A skipped field costs a tap; a wrong one misstates your history.

`rules_payload()` now also serves `negative` / `affirmative` / `source_patterns`.
Anything added there must also land on `RulesPayload` in `ios/Apply/Models.swift` —
the app re-encodes that struct for JS, so an un-modelled key is silently dropped.

**Widget engine fixes (iOS + extension, same day).** Greenhouse's newer controls are
`input[text]` + hidden input + React listbox, not `<select>`. Fixes:

1. Open react-select with **mousedown on the control container**; commit the option
   with mousedown too (bare `.click()` opens nothing / picks nothing).
2. Keep the human-facing label apart from placeholder/name/id so anchored rules
   (`^\s*name\s*$`) survive.
3. Ashby Yes/No as a **two-button pair** (`input[checkbox]` / bare `<button>`).
   `fillButtonGroups` refuses any button with `b.form` set — a `<button>` with no
   `type` defaults to submit; never relax that.
   4. **`fillCombobox` reads the unfiltered menu first**, and only types to filter when
   that finds nothing. Typing `"company career site"` into a literal filter emptied
   Affirm's list (no option contains those words). There is **no**
   `tests/fixtures/forms/react_widgets.html` in this tree — pin new widget shapes
   under `tests/fixtures/forms/` when a live run breaks, rather than inventing a
   fixture name in the docs.

**Affirm live result (one tap):** first/last name, email, country, phone, LinkedIn,
GitHub, preferred name, both sponsorship questions, state, how-did-you-hear,
previously-employed — **13 fields**. Both demographic dropdowns blank (correct).
Sitting on Submit application.

**Still yours on that form:** pronouns (no identity value set) and the résumé upload
(phone autofill can't attach files; see next).

**Still open — Playwright worker.** `worker/run.py` still has the old widget
handling. The phone and extension are fixed; the Playwright filler is the one
surface that can attach the résumé, and it's the path that must match before
phone-triggered auto-submit is at parity. Port the unfiltered-menu + react-select
mousedown sequence there next.

**Also fixed the same day (label-side, not Affirm-specific):** phone rule word-bounded
(`tel` was matching "tell us…"); work-auth / sponsorship ordered above country;
`your name` anchored so "how do you pronounce your name?" stays an essay;
discipline no longer claims "a major project".

**Prod blockers found while live-testing** (see memory
`prod-blockers-2026-07-27.md`): invalid Fly `ANTHROPIC_API_KEY` (drafts fail-open to
template); staged postings never re-checked for liveness; resume compile needs
`TECTONIC_BIN=/usr/local/bin/tectonic`; iOS ships no `APPLY_API_TOKEN` so the phone
falls back to bundled rules (toast: "offline rules").

---

## 3. Key endpoints

- `GET /` dashboard (now includes **🤖 In flight** + **🧠 What I know about you**)
  · `GET /health` (status + every feature flag)
- `POST /slack/events` (Slack inbound)
- `GET /train` swipe trainer · `GET /train/deck` (`mode=best|mix|learn`) · `POST /train/label` · `POST /train/summaries`
- `GET /apply` review page · `GET /apply/data` · `POST /apply/stage|package|mark|remove`
- `GET /apply/resume` (PDF) · `POST /apply/answer/save|redraft` (per-question, by `index`)
- `GET/POST /apply/identity` · `POST /apply/answer` (single question; used by extension)
- **Submit pipeline:** `POST /apply/autosubmit`, `GET /apply/request`, `POST /apply/request/approve|cancel`
- **Mobile:** `GET /apply/rules` (shared autofill rules) · `GET /apply/inflight` ·
  `GET/POST /apply/knowledge` + `/apply/knowledge/remove` · `POST /apply/device`
  (+`/remove`) for push tokens. `/apply/data` rows now carry `why`/`reasons`/`concerns`.
- **Worker (token-gated `X-Apply-Token`):** `POST /worker/claim`, `/worker/claim_approved`, `/worker/preview`, `/worker/result`

---

## 4. User identity & data (IMPORTANT for continuity)

- The user's **Slack user id is `U07LVJVD4PL`**. Slack chat keys on this id.
- The user's **trained data (257 swipe labels + re-ranker model + profile)** lived
  under user **`local`** in the **worktree's local `job_search.db`** (dev sessions),
  **not** prod. It was exported to `~/Documents/job-search-tool/brain.db` (gitignored)
  via `scripts.export_user`, to be imported into prod under the Slack id via
  `scripts.import_user`.
- **Web pages + extension default to the busiest user** (`local`). Guidance: use
  `?user=U07LVJVD4PL` on web pages, set the extension's user id to `U07LVJVD4PL`, and
  set `JOB_ALERT_USER=U07LVJVD4PL`, so everything lives under one id.
- Tools: `scripts.migrate_user <src> <dst>` (merge ids within a DB),
  `scripts.export_user` / `scripts.import_user` (move a "brain" across DBs).
  Run on Fly from `/app`: `flyctl ssh console -C "sh -c 'cd /app && python -m scripts.X …'"`.

---

## 5. ⏳ OUTSTANDING — user actions to finish going live

> **As of 2026-07-27, prod is running `worker-robust-fill`, which is ahead of
> `main`.** Do **not** `flyctl deploy` from `main` until that branch is merged, or
> the new identity keys / rules / widget fixes roll back under a live phone.
> Check `GET /health` first. The assistant has no Fly auth in-session.

1. **Merge `worker-robust-fill` → `main`**, then deploy: `flyctl deploy -a job-search-tool`.
2. **Set Fly secrets** (verify with `flyctl secrets list -a job-search-tool`):
   - Required: `ANTHROPIC_API_KEY` (**currently invalid in prod — fix this**),
     `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`.
   - Turn on the brain: `RERANKER_ENABLED=true`, `DECK_TLDR_ENABLED=true` (the latter
     is REQUIRED for `llm_fit` in discovery + per-question answers).
   - Recommended: `APPLY_API_TOKEN=<random>` (protects `/apply/identity` + gates the
     worker; the extension *and* the iOS app must send the same token),
     `JOB_ALERT_USER=U07LVJVD4PL`, `TECTONIC_BIN=/usr/local/bin/tectonic`.
   - Optional: `JOB_AUTO_QUEUE_THRESHOLD=0.85` (auto-stage strong matches),
     `EMBEDDING_ENABLED=true`+`VOYAGE_API_KEY` (semantic sort — low value),
     `SERPAPI_API_KEY`+`JOB_WIDE_AGGREGATOR_ENABLED=true`+aggregator in `JOB_SOURCES_ENABLED` (paid),
     `APOLLO_API_KEY` (recruiters).
3. **Import the trained brain** (if not done): upload `brain.db` to `/data/brain.db`
   (base64 pipe over `flyctl ssh console`), then
   `flyctl ssh console -C "sh -c 'cd /app && python -m scripts.import_user /data/brain.db U07LVJVD4PL'"`.
   Verify: `reranker.load_model('U07LVJVD4PL')` → labels: 257. Re-export any
   `brain.db` made before 2026-07-26 so `posting_summaries` comes along.
4. **Upload base resumes** so tailoring works: `swe.tex` + `aiml.tex` (the user has
   them at `~/Documents/job-search-tool/resumes/`) → `/data/resumes/` on the volume.
5. **Load the extension:** `chrome://extensions` → Developer mode → Load unpacked →
   `extension/` **from the worktree** (not stale `main`). Set server URL, user id
   `U07LVJVD4PL`, the `APPLY_API_TOKEN`, and identity (incl. `referral_source` /
   `previously_employed` / `pronouns`). Reload after any `extension/` change.
6. **Deploy + live-test the submit worker** (the last mile — see `worker/README.md`):
   port the §2.G widget fixes into `worker/run.py` first, then
   `fly deploy -c worker/fly.toml`. Run it **headful** (`WORKER_HEADLESS=false`)
   against a few real Greenhouse/Lever/Ashby forms; note what lands in "skipped".

---

## 6. Config flags (env vars; defaults in `app/config.py`)

`RERANKER_ENABLED` (false) · `RERANKER_OUTCOME_WEIGHTING` (true) · `DECK_TLDR_ENABLED`
(false) · `EMBEDDING_ENABLED` (false) + `VOYAGE_API_KEY` · `GHOST_FILTER_ENABLED`
(true) · `ELIGIBILITY_FILTER_ENABLED` (true) · `ELIGIBILITY_LLM_ENABLED` (false) ·
`JOB_RELEVANCE_THRESHOLD` (0.6) · `JOB_AUTO_QUEUE_THRESHOLD` (0.0 = off) ·
`JOB_ALERT_MODE` (digest) · `JOB_ALERT_USER` ("") · `RESUME_TAILOR_ENABLED` (true) ·
`APPLY_API_TOKEN` ("") · `APPLY_CORS_ORIGINS` ("*") · SerpApi/Apollo gates ·
`PUSH_ENABLED` (false) + `APNS_KEY_ID`/`APNS_TEAM_ID`/`APNS_BUNDLE_ID`/
`APNS_KEY_PATH`/`APNS_USE_SANDBOX` (push is a no-op until all are set).

**Worker-side** (read by `worker/`, a separate process — set them in the worker's
own environment, not the app's): `BASE_URL` · `APPLY_API_TOKEN` (must match the main
app's) · `WORKER_HEADLESS` (true) · `WORKER_AGENT` (false) · `AGENT_MODEL`
(`claude-opus-4-8`) · `AGENT_MAX_TOKENS` (4096) · `AGENT_MAX_STEPS` (40) ·
`AGENT_KEEP_STEPS` (6) · `AGENT_TOKEN_BUDGET` (150000) ·
`AGENT_RATE_LIMIT_PER_MIN` (30). All documented in `.env.example`.

---

## 7. Run / test locally

```bash
VP=/Users/rahilsheth/Documents/job-search-tool/.venv/bin/python
$VP -m pytest -q                      # full suite (collects 824 as of 2026-08-26)
$VP -m uvicorn app.main:app --reload  # local server on :8000
# CI: .github/workflows/pytest.yml (PRs + non-main pushes).
#     Push to main runs the same job, then Fly deploy only if it passes.
```
`tests/conftest.py` forces offline (neutralizes ANTHROPIC/SLACK/APOLLO/SERPAPI/
VOYAGE/TWILIO + stubs dotenv) and uses a temp DB. Tests that need a real flag set it
via `monkeypatch.setenv(...)` **then `config.get_settings.cache_clear()`** (the
settings are `@lru_cache`d).

Active checkout for the 2026-07-27 work:
`/Users/rahilsheth/Documents/job-search-tool/.claude/worktrees/admiring-turing-682e7e`
(branch `worker-robust-fill`). The main workspace tree is behind.

---

## 8. Known limitations / rough edges

- **Submit worker**: no longer untested. `tests/test_worker_fill.py` drives real
  headless Chromium against fixtures in `tests/fixtures/forms/` (iframe, reveal,
  ARIA combobox, late SPA paint, EEO section, two-step Next, Ashby Yes/No buttons)
  and asserts the two invariants — *never submits*, *never fills hard-blocked EEO*.
  Custom dropdowns and Yes/No radio groups fill in the worker and the phone/extension
  engines. Headful live-test remains the acceptance step (`worker/LIVE_TEST.md`);
  fixtures are not live ATS DOMs. Submit-button detection is still heuristic
  (searches every frame); holds one job open while awaiting approval (fine for
  personal volume). See `worker/README.md` + `worker/LIVE_TEST.md`.
- **Embeddings** add ~0 to personalization (label imbalance was the bottleneck, not
  features) — don't expect magic.
- **Login-gated sites** (Workday, LinkedIn Easy Apply) are out of scope for the
  public-form worker — those fall back to the desktop extension.
- **Résumé upload from the phone autofill** is not possible (WKWebView can't set
  `<input type=file>`). Attach manually, or wait for the Playwright worker path.
- **In-app browser holds one form at a time** — popping back destroys the
  `WebViewModel`, so you can't line several filled applications up for review.

---

## 9. Gotchas for the next session

- **`.env.example` keeps reverting** to a stale (shorter) version in the worktree
  working copy. **Always `git checkout origin/main -- .env.example` before committing.**
- **Never put a live secret in `.env.example` — it's tracked.** A real Anthropic key
  and `APPLY_API_TOKEN` were once pasted there as part of a worker run command; caught
  before it was committed. Live values belong in `.env` (gitignored). Every worker and
  agent variable is now documented in `.env.example` with empty values so there's no
  reason to edit it.
- **A restored brain can be quietly worse than it looks.** `posting_summaries` is
  keyed by `source:external_id`, not `user_id`, so it wasn't part of the brain export
  until 2026-07-26 — a restore brought the labels and the model across but dropped the
  LLM judgements they depend on, costing ~0.06 AUC with no error anywhere. Fixed in
  `usermerge.py`; re-export any `brain.db` made before that date.
- **Run scripts on Fly from `/app`** (SSH lands in `/`): `cd /app && python -m scripts.X`.
- **`scripts/` must stay OUT of `.dockerignore`** (it was excluded → operational
  scripts failed on Fly; fixed in PR #20 — don't re-add it).
- **Use the venv python** (`.venv/bin/python`), not `python` (pyenv 3.13 missing).
- **The worker is fixture-tested** in `tests/test_worker_fill.py` (real Chromium,
  local HTML). That does **not** prove live Greenhouse/Lever/Ashby. Headful
  checklist: `worker/LIVE_TEST.md`. `fieldmatch.py` is shared with the extension
  and iOS, so fixes there improve all three.
- Workflow this session: branch off `origin/main` → commit → push → `gh pr create` →
  `gh pr merge --merge`. 17 PRs (#11–#27) this session, all merged.

---

## 10. Roadmap / what's next (if continuing the build)

The vision is **complete**; next steps are tuning + polish, not new architecture:
1. **Port §2.G widget fixes into `worker/run.py`** and live-test headful — this is
   the path that attaches the résumé and makes phone-triggered auto-submit match
   what ⚡ Autofill already does. Add a fixture for whatever the live run breaks on.
1b. ✅ **Extension DOM suite** — `tests/test_extension_autofill.py` drives shipping
   `content.js` against the same fixtures (identity, native select, radios, EEO,
   never-submit, fieldLabel contract). It does **not** yet click Ashby Yes/No
   buttons or Greenhouse react-select widgets; those stay iOS-covered. There is
   no `react_widgets.html` fixture in this tree.
2. ✅ **Resume upload** is wired on the worker (`fieldmatch.is_resume_field`) —
   still needs the widget port + a headful Affirm/Greenhouse pass to prove it.
3. ✅ **Preview screenshot** is wired — the worker sends a full-page JPEG data URL as
   `preview.screenshot_url`; the `/apply` review page already renders it.
4. **Fill the remaining identity gaps** the user still has to supply once
   (pronouns, referral_source canonical, education, salary, start date, etc. —
   see §11). After that, every *fact* field is fillable; essays stay on the
   knowledge store / drafted answers.
5. Optional: cover-letter generation; re-check staged postings for liveness;
   ship `APPLY_API_TOKEN` into the iOS app so it stops using offline rules;
   batch triage swipe surface for real matches.

See also memory: `matching-v2-plan.md`, `build-sequencing.md`,
`ios-autofill-field-gaps.md`, `prod-blockers-2026-07-27.md` in
`~/.claude/projects/-Users-rahilsheth-Documents-job-search-tool/memory/` for the
full decision history.

---

## 11. Identity checklist — what to tell me so every question fills

The autofill only paints values that exist in `applicant_json`. Rules without a
stored value → skip (one tap). Give me these once; I'll write them via
`POST /apply/identity` (or Slack `remember` for essay facts).

### Facts (identity keys — fill every form)
Paste values for any blank ones. Canonical `referral_source` must be one of the
six keys below, **not** "Affirm's Career Site".

| Key | Example / notes |
|---|---|
| `first_name` / `last_name` / `preferred_name` | legal + what you go by |
| `pronouns` | e.g. `he/him` — **still blank on Affirm** |
| `email` / `phone` | |
| `address` / `city` / `state` / `zip` / `country` | country as the form spells it (`United States`) |
| `linkedin` / `github` / `portfolio` | full URLs |
| `school` / `degree` / `discipline` / `gpa` / `grad_year` | |
| `current_company` / `current_title` / `years_experience` | |
| `salary_expectation` / `start_date` | desired figure; earliest available |
| `work_authorized` | `true` / `false` |
| `needs_sponsorship` | `true` / `false` |
| `willing_to_relocate` | `true` / `false` |
| `previously_employed` | almost always `false` |
| `referral_source` | **exactly one of:** `company career site`, `job board`, `linkedin`, `referral`, `recruiter`, `event` |
| `gender` | canonical: `male` / `female` / `nonbinary` |
| `race_ethnicity` | canonical: `asian` / `white` / `black` / `hispanic` / `native_american` / `pacific_islander` / `two_or_more` |
| `hispanic_latino` / `veteran_status` / `disability_status` | bool → Yes/No |

**2026-07-27 values on file (local worktree DB):** BCBS / Software Developer / 3 yrs,
`$30/hr`, ASAP, Vernon Hills address, portfolio URL, gender=`male`,
race_ethnicity=`asian`, hispanic/veteran/disability=`No`. **Prod still needs the
same write + a deploy of the opt-in EEO code** before the phone fills demographics.

Sexual orientation, religion, DOB, transgender, citizenship status remain
never-fill (no identity key).

### Essays / "tell us about yourself" (knowledge store, not identity)
These never get a single stored string — teach reusable answers:

- `remember answer to "Why do you want to work here?": …`
- `remember answer to "Tell us about yourself": …`
- `remember project: …` / achievements / strengths

Ambiguous multi-option Yes (e.g. "Yes, as an intern" vs "Yes, as full-time") stays
manual on purpose — say which if a company asks and we'll add a finer key.

### Résumé
Phone autofill cannot attach a file. Either upload per form, or finish the
Playwright worker port so auto-submit attaches the tailored PDF.
