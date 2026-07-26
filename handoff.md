# Job Search Intelligence — Engineering Handoff

> Pick-up doc for a fresh session. Last updated **2026-07-25**.
> Test suite: **761 passed, 15 skipped**.
> ⚠️ The newest work is on branch **`worker-robust-fill`**, not yet merged to
> `main` — worker fixture tests, Slack-native approve, the knowledge store, the
> discovery accuracy pass, and the **mobile build-out** (shared autofill rules, four
> tabs, push). See `OVERNIGHT_NOTES.md` for what still needs you.

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
- **Transport:** **Slack** is primary (`POST /slack/events` inbound + `SlackSender`
  outbound). Twilio SMS is dormant, one config flip away. Engine is transport-agnostic.
- **Hosting:** **Fly.io**, app `job-search-tool`, region `iad`, URL
  `https://job-search-tool.fly.dev`. One always-warm 512MB machine (so the in-process
  APScheduler reminder + discovery loops keep ticking). SQLite on a **1GB persistent
  volume at `/data/job_search.db`** (`DATABASE_PATH` set in the Dockerfile).
- **Repo:** GitHub `rsheth8/job-search-tool` (private). `gh` account `rsheth8`,
  Fly account `rahilsheth05@gmail.com`. Redeploy: `flyctl deploy -a job-search-tool`
  from the repo root on `main`. Migrations are idempotent (run at import).
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
  `stretch` from the summarizer). `llm_fit` is the #1 signal (+0.06 AUC). Has a
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
- **Resume tailoring** (`resume_tailor.py`): per-posting tailored one-page PDF
  (SWE vs AI/ML variant), Tectonic-compiled, cached. Needs base `.tex` on the volume
  (see §5).

### D2. The iPhone app — `ios/` (SwiftUI + WKWebView)
Four tabs: **Apply** (matches + `Prepare application` to stage, each row showing
*why* it surfaced), **In flight** (the worker's previews + the approve/cancel gate),
**About me** (`knowledge` + the coverage audit), **Settings** (backend + push).
An in-app browser opens the real form; ⚡ Autofill fills it. Push notifications
(APNs) fire on new matches and on a preview awaiting approval, deep-linking to the
right tab. See `ios/README.md`.

### E. The autofill extension — `extension/` (MV3, load unpacked)
A browser extension (Chrome/Edge/desktop; Safari-packageable for iOS). Focus a
field → inline chip with the value or a drafted answer. **⚡ Autofill this page**
button fills everything recognized at once (text, dropdowns, Yes/No radios). Options
page edits identity (syncs to server). Talks to `/apply/identity` + `/apply/answer`.

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

> All code is merged. These are deploy/setup steps the assistant **cannot** do
> (no Fly auth in-session). **Verify what's actually been done — the user may have
> completed some of these already.** Check `GET /health` first.

1. **Deploy latest `main`:** `cd ~/Documents/job-search-tool && git checkout main && git pull && flyctl deploy -a job-search-tool`.
2. **Set Fly secrets** (verify with `flyctl secrets list -a job-search-tool`):
   - Required: `ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`.
   - Turn on the brain: `RERANKER_ENABLED=true`, `DECK_TLDR_ENABLED=true` (the latter
     is REQUIRED for `llm_fit` in discovery + per-question answers).
   - Recommended: `APPLY_API_TOKEN=<random>` (protects `/apply/identity` + gates the
     worker; the extension must send the same token), `JOB_ALERT_USER=U07LVJVD4PL`.
   - Optional: `JOB_AUTO_QUEUE_THRESHOLD=0.85` (auto-stage strong matches),
     `EMBEDDING_ENABLED=true`+`VOYAGE_API_KEY` (semantic sort — low value),
     `SERPAPI_API_KEY`+`JOB_WIDE_AGGREGATOR_ENABLED=true`+aggregator in `JOB_SOURCES_ENABLED` (paid),
     `APOLLO_API_KEY` (recruiters).
3. **Import the trained brain** (if not done): upload `brain.db` to `/data/brain.db`
   (base64 pipe over `flyctl ssh console`), then
   `flyctl ssh console -C "sh -c 'cd /app && python -m scripts.import_user /data/brain.db U07LVJVD4PL'"`.
   Verify: `reranker.load_model('U07LVJVD4PL')` → labels: 257.
4. **Upload base resumes** so tailoring works: `swe.tex` + `aiml.tex` (the user has
   them at `~/Documents/job-search-tool/resumes/`) → `/data/resumes/` on the volume.
5. **Load the extension:** `chrome://extensions` → Developer mode → Load unpacked →
   `extension/`. Set server URL, user id `U07LVJVD4PL`, the `APPLY_API_TOKEN`, and
   identity. Reload after any `extension/` change.
6. **Deploy + live-test the submit worker** (the last mile — see `worker/README.md`):
   `fly launch --no-deploy --name job-search-worker --copy-config -c worker/fly.toml`,
   set `BASE_URL` + `APPLY_API_TOKEN` secrets, `fly deploy -c worker/fly.toml`. Run it
   **headful** (`WORKER_HEADLESS=false`) against a few real Greenhouse/Lever/Ashby
   forms first; note what lands in "skipped" and tune `app/fieldmatch.py`.

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

---

## 7. Run / test locally

```bash
VP=/Users/rahilsheth/Documents/job-search-tool/.venv/bin/python
$VP -m pytest -q                      # full suite (699 passed, 15 skipped)
$VP -m uvicorn app.main:app --reload  # local server on :8000
```
`tests/conftest.py` forces offline (neutralizes ANTHROPIC/SLACK/APOLLO/SERPAPI/
VOYAGE/TWILIO + stubs dotenv) and uses a temp DB. Tests that need a real flag set it
via `monkeypatch.setenv(...)` **then `config.get_settings.cache_clear()`** (the
settings are `@lru_cache`d).

---

## 8. Known limitations / rough edges

- **Submit worker**: no longer untested. `tests/test_worker_fill.py` drives real
  headless Chromium against fixtures in `tests/fixtures/forms/` (iframe, reveal,
  ARIA combobox, late SPA paint, EEO section) and asserts the two invariants —
  *never submits*, *never fills EEO*. Custom dropdowns and Yes/No radio groups now
  fill. What's still open: real ATS DOMs may not match the fixtures, so a headful
  live-test remains the acceptance step; submit-button detection is still heuristic
  (now searches every frame); holds one job open while awaiting approval (fine for
  personal volume). See `worker/README.md` + `worker/LIVE_TEST.md`.
- **Embeddings** add ~0 to personalization (label imbalance was the bottleneck, not
  features) — don't expect magic.
- **Login-gated sites** (Workday, LinkedIn Easy Apply) are out of scope for the
  public-form worker — those fall back to the desktop extension.

---

## 9. Gotchas for the next session

- **`.env.example` keeps reverting** to a stale (shorter) version in the worktree
  working copy. **Always `git checkout origin/main -- .env.example` before committing.**
- **Run scripts on Fly from `/app`** (SSH lands in `/`): `cd /app && python -m scripts.X`.
- **`scripts/` must stay OUT of `.dockerignore`** (it was excluded → operational
  scripts failed on Fly; fixed in PR #20 — don't re-add it).
- **Use the venv python** (`.venv/bin/python`), not `python` (pyenv 3.13 missing).
- **The worker can't be unit-tested** — it drives a browser. Test it headful with the
  user. `fieldmatch.py` is shared with the extension, so fixes there improve both.
- Workflow this session: branch off `origin/main` → commit → push → `gh pr create` →
  `gh pr merge --merge`. 17 PRs (#11–#27) this session, all merged.

---

## 10. Roadmap / what's next (if continuing the build)

The vision is **complete**; next steps are tuning + polish, not new architecture:
1. **Tune the worker** against real forms (the actual last mile — run it
   `WORKER_HEADLESS=false`, watch the per-field log, tighten `fieldmatch`/`submit_form`).
   Fixtures now catch most of this before you open a browser; add a fixture for
   whatever the live run breaks on.
1b. **Mirror the `fieldmatch` changes into `extension/content.js`** — the broadened
   label variants and the widened EEO never-fill list. The Python side is the source
   of truth; the extension's `RULES` have drifted behind it. See `OVERNIGHT_NOTES.md`.
2. ✅ **Resume upload** is wired — the worker fetches `/apply/resume` and attaches it to
   the resume/CV file input (`fieldmatch.is_resume_field`). Tune on multi-upload forms.
3. ✅ **Preview screenshot** is wired — the worker sends a full-page JPEG data URL as
   `preview.screenshot_url`; the `/apply` review page already renders it.
4. Optional: cover-letter generation; more application question types; Slack-native
   approve (vs the web page); a batch triage swipe surface for real matches.

See also memory: `matching-v2-plan.md` and `build-sequencing.md` in
`~/.claude/projects/-Users-rahilsheth-Documents-job-search-tool/memory/` for the
full decision history.
