# Handoff — Job Discovery (Phase 1 complete, Phase 2 next)

> For a fresh Claude Code session rooted in `/Users/rahilsheth/Documents/job-search-tool`.
> Companion to `handoff.md` (the pre-discovery engineering doc — still accurate for
> the tracker core). This file covers the **job-discovery** feature added 2026-06-04.

## TL;DR

The conversational tracker can now **discover jobs and alert you on Slack** when new
ones drop — not just log apps you tell it about. Phase 1 is **built, tested
(274 passing), and verified end-to-end against live job boards**. Next up: **Phase 2
— assisted apply.**

## Prereqs (read first)

- **This is now a git repo** (it wasn't before). Baseline + feature commits are on `main`.
  Run `git log --oneline` to see the trail.
- Use the existing **`.venv`** (Python 3.12/3.13, NOT 3.14). Tests: `.venv/bin/python -m pytest -q`.
- Tests run **offline** (conftest forces no API keys → free heuristic paths). Never hit live APIs in tests.
- Full plan: `~/.claude/plans/let-s-plan-our-next-golden-moler.md`.

## What Phase 1 added

**New modules**
- `app/jobsources/` — pluggable source adapters. `base.py` (`JobPosting` dataclass,
  resilient `get_json`, HTML/`strip_html` helpers), `greenhouse.py`, `lever.py`,
  `ashby.py` (free, no-auth public JSON; each has a pure `_parse()` for fixture tests),
  `__init__.py` (`SOURCES` registry + `fetch_source`).
- `app/jobstore.py` — tracked-board CRUD + posting persistence. Dedupe via
  INSERT-OR-IGNORE on `(user_id, source, external_id)` → a posting is scored/alerted once.
- `app/profile.py` — per-user job-search profile (roles/keywords/locations/seniority/
  resume), field-wise upsert + `profile_text()` render.
- `app/matcher.py` — free `prefilter()` (keyword/location) + `score()` (0..1). Batched
  Haiku call when keyed, else free `_heuristic_score()` (the CI path). Empty profile → 0.5.
- `app/discovery.py` — `tick(user_id)` (fetch→dedupe→prefilter→cap→score→persist→Slack
  alert via `reminders.get_sender()`), `run_all()` sweep, `seed_board()` (baseline existing
  roles on first track, no alerts), `resolve_board()` (auto-detect a company's board across
  free sources), one-shot `python -m app.discovery`.

**Edited**
- `app/db.py` — `tracked_companies`, `job_postings`, `job_search_profile` tables + indexes.
- `app/config.py` — `job_*` settings (`job_sources`, `job_poll_seconds`,
  `job_relevance_threshold`, `job_max_scored_per_tick`, `job_alert_user`).
- `app/scheduler.py` — second APScheduler job `discovery_tick` alongside `reminder_tick`.
- `app/main.py` — `/health` `discovery` block.
- `app/intents.py` — `TRACK`, `JOBS`, `PROFILE` intents.
- `app/router.py` — heuristic detection (placed before APPLY/QUERY/LIST; APPLY-guard on
  JOBS) + Claude intent docs + few-shots.
- `app/engine.py` — `_do_track` / `_do_jobs` / `_do_profile` + `_split_profile_criteria`;
  dispatch in `_start`; nav-interrupt set; MENU.
- `tests/conftest.py` — resets `matcher` LLM singletons.
- `.env.example`, `README.md` — documented.
- `app/deadlines.py` — unrelated pre-existing time-bomb test fixed (injectable `now`).

## How it works (user-facing)

1. `I'm looking for new grad SWE roles, remote or NYC` → sets the match profile.
2. `track openings at stripe` → `resolve_board()` finds the public board (Greenhouse/
   Lever/Ashby), `seed_board()` baselines current roles **silently** (no first-track spam).
3. Background loop (`discovery_tick`, every `job_poll_seconds`) alerts on NEW matches
   above `job_relevance_threshold`, via the same Slack sender as reminders.
4. `any new jobs` (browse) · `what am I tracking` / `stop tracking X` (manage).

**Cost controls:** free sources first; LLM only sees pre-filtered postings, batched, capped
per tick; dedupe means score/alert once; `$0` with no key (heuristic).

## Verify

```bash
.venv/bin/python -m pytest -q                 # 274 passing
.venv/bin/python -m app.discovery             # one-shot tick (self-inits DB)
# live adapters confirmed: Greenhouse(stripe ~485), Ashby(ramp ~111); Lever fixture-tested.
# /health → "discovery" block (sources, tracked boards, last tick, posting counts).
```

## Phase 2 — assisted apply (DO THIS NEXT)

Goal: from Slack, `apply <#>` (the alert already prints `#<id>`) → reply with the apply
link + pre-drafted answers (from the profile), and log it as Applied. **No auto-submit.**

Concrete steps:
1. **Intent:** add `APPLY_JOB` to `app/intents.py`. Heuristic in `router.py`: match
   `apply <n>` / `apply to the <company> one` (capture the posting id in `message` or a new
   field; simplest: put the integer id in `message`). Add a Claude few-shot.
2. **Engine `_do_apply_job(user_id, p)`:** `jobstore.get_posting(user_id, id)` → if found,
   draft answers with Claude reusing the `app/outreach.draft_outreach()` pattern + the
   profile (resume_summary); reply with `posting.url` + the draft. Then
   `store.create_application(user_id, posting.company, posting.title, source="discovery")`
   and `jobstore.mark_posting_status(id, "applied")`. Keep a heuristic/no-key fallback that
   still returns the link + a templated note (never block the user).
3. **Source:** add `app/jobsources/rss.py` (RSS / HN "Who is hiring") + register in
   `SOURCES`; pure `_parse()` + fixture test.
4. **Tests:** mirror `tests/test_jobs_intents.py` — route `apply 2`, `_do_apply_job` logs an
   application + flips posting to `applied`, draft present; RSS `_parse` on a fixture.

## Phase 3 / 4 (deferred, behind flags + budget caps — Apollo-style)

- Paid aggregator (Indeed/Google Jobs via SerpApi-style API): `app/jobsources/aggregator.py`,
  off by default, daily cap.
- LinkedIn: `app/jobsources/linkedin.py`, off by default.

## To go always-on (Fly)

`Dockerfile`/`fly.toml` exist. `fly launch --no-deploy` → `fly volumes create data --size 1`
→ `fly secrets set ANTHROPIC_API_KEY=… SLACK_BOT_TOKEN=… SLACK_SIGNING_SECRET=… APOLLO_API_KEY=…`
→ `fly deploy`; point Slack Events URL at `https://<app>.fly.dev/slack/events`.
**Alerts only DM when `SLACK_BOT_TOKEN` is set** (else they log via LogSender).

## Gotchas

- **Per-tick scoring cap** (`job_max_scored_per_tick`, default 40): a big board's
  prefiltered backlog is scored across several ticks — extra alerts on early ticks are the
  carry-over working, not a dedupe bug. `seed_board` prevents the *first-track* storm.
- **`seeded` status** = baselined-but-not-alerted; excluded from `any new jobs` (which shows
  `alerted`/`new`).
- Heuristic router/matcher are the **CI path**; keep them working (free fallback).
- Anthropic only, **Haiku 4.5** — don't change models without asking (per `handoff.md` §10).
