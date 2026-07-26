# Deploying to Fly.io

Always-on hosting gives you a **stable Slack webhook URL** (no more re-verifying
after every ngrok restart) and makes **outbound reminders actually fire** — the
APScheduler loop only delivers due reminders while the server is up.

## Why these settings

- **Persistent volume.** SQLite is a single file. Without a volume, every deploy
  or machine restart wipes your data. `fly.toml` mounts a volume named `data` at
  `/data` and points `DATABASE_PATH` there.
- **One warm machine.** Reminders are scheduled in-process. `auto_stop_machines
  = "off"` + `min_machines_running = 1` keep a machine alive so the scheduler
  keeps ticking. (Don't scale to multiple machines — you'd get duplicate
  reminders and split SQLite state.)

## One-time setup

```bash
# 1. Install + log in
brew install flyctl
fly auth login

# 2. Create the app from the existing fly.toml (don't deploy yet).
#    Accept the generated app name or keep "job-search-tool"; decline Postgres/Redis.
fly launch --no-deploy

# 3. Create the persistent volume in the same region as the app.
fly volumes create data --size 1 --region iad

# 4. Set secrets (these are NOT baked into the image — .env is .dockerignored).
fly secrets set \
  ANTHROPIC_API_KEY=sk-ant-... \
  SLACK_BOT_TOKEN=xoxb-... \
  SLACK_SIGNING_SECRET=... \
  JOB_ALERT_USER=U07LVJVD4PL \
  RERANKER_ENABLED=true \
  DECK_TLDR_ENABLED=true \
  APPLY_API_TOKEN="$(openssl rand -hex 16)"
```

**Required:** `ANTHROPIC_API_KEY` + the two `SLACK_*` values.

**`JOB_ALERT_USER`** is your Slack user id (`U07LVJVD4PL`) — required for discovery
digests, and it's what keys everything to one identity. Set it, or the web pages and
the phone end up reading a different user's data than Slack writes to.

**Turn the brain on.** `RERANKER_ENABLED=true` activates the personalized re-ranker;
`DECK_TLDR_ENABLED=true` is **required for it to be any good** — `llm_fit` is its
strongest single feature and it comes from the summarizer. Without these two the
matcher falls back to a base score that measures ~0.54 AUC against your actual taste,
i.e. close to a coin flip. Don't tune `JOB_RELEVANCE_THRESHOLD` while they're off.

**`APPLY_API_TOKEN`** protects `/apply/identity` and gates the worker. Generate it
once and keep it — the browser extension, the iOS app, and the worker all have to
send the same value. Read it back later with `fly secrets list` (which shows only a
digest) or just regenerate and update all three.

Optional: `APOLLO_API_KEY` (recruiter discovery), and for paid broad search
`SERPAPI_API_KEY` + `JOB_WIDE_AGGREGATOR_ENABLED=true` + `aggregator` appended to
`JOB_SOURCES_ENABLED`.

## Restore your trained brain (do this before you start swiping again)

The volume is the database. If you deleted the old app and its volume, prod starts
**empty** — but the expensive part, your hand-labelled swipes, lives in `brain.db` at
the repo root (gitignored, exported via `scripts.export_user`). Put it back:

```bash
# Upload the export, then import it under your Slack id.
fly ssh sftp put -a job-search-tool brain.db /data/brain.db
fly ssh console -a job-search-tool \
  -C "sh -c 'cd /app && python -m scripts.import_user /data/brain.db U07LVJVD4PL'"

# Verify — expect n_labels: 257
fly ssh console -a job-search-tool \
  -C "sh -c 'cd /app && python -c \"from app import reranker; print(reranker.load_model(\\\"U07LVJVD4PL\\\"))\"'"
```

Two gotchas, both of which have bitten before:

- **SSH lands in `/`, not `/app`.** Every `python -m scripts.X` has to `cd /app` first.
- **`scripts/` must stay out of `.dockerignore`.** It was excluded once and every
  operational script silently vanished from the image. Fixed in PR #20 — don't re-add.

What `brain.db` does **not** carry back: applications you logged through Slack against
the old prod DB, knowledge-store entries, and push device tokens. Those were only ever
on the deleted volume.

Wide discovery (RSS + ATS directory rotation) is configured in `fly.toml` `[env]`.
Set your match criteria once in Slack, e.g. `"I'm looking for new grad SWE roles,
remote or NYC"`. Optional paid broad search: add `SERPAPI_API_KEY`, set
`JOB_WIDE_AGGREGATOR_ENABLED=true`, and append `aggregator` to `JOB_SOURCES_ENABLED`.

Slack bot scopes: `chat:write`, `files:write` (for resume PDF attachments on
`apply <#>`). After adding scopes, **Reinstall App** and update `SLACK_BOT_TOKEN`.

## Resume base files (one-time)

Tailored resumes need `swe.tex` and `aiml.tex` on the volume (not in git):

```bash
fly ssh console -a job-search-tool -C "mkdir -p /data/resumes"
# sftp put refuses to overwrite — remove first when updating an existing file.
fly ssh console -a job-search-tool -C "rm -f /data/resumes/swe.tex /data/resumes/aiml.tex"
fly ssh sftp put -a job-search-tool resumes/swe.tex /data/resumes/swe.tex
fly ssh sftp put -a job-search-tool resumes/aiml.tex /data/resumes/aiml.tex
fly ssh console -a job-search-tool -C "ls -la /data/resumes/"
```

Do **not** use `scp root@*.fly.dev` — Fly closes that connection. Use `fly ssh sftp put` instead.

Until these are on the volume, `GET /apply/resume` and the iOS **Download PDF** button
return 404, and the worker logs the resume field as skipped. Everything else works.

Tailored outputs cache under `/data/resumes/tailored/`. Tectonic is in the Docker image.

## Deploy

```bash
fly deploy
fly status                       # confirm one machine is running
curl https://<app>.fly.dev/health   # router, scheduler, reminder_delivery: slack
```

## Repoint Slack (once)

Slack app → **Event Subscriptions** → Request URL:

```
https://<app>.fly.dev/slack/events
```

Re-verify (the server validates the challenge), keep `message.im` (+
`app_mention`) subscribed, and save. Because the URL is now stable, you never
have to do this again.

## Smoke test

1. DM the bot: `applied notion swe ii` → expect a "Logged" reply.
2. `what should I follow up on` → expect a ranked list.
3. `apply <#>` on a discovered job → link + draft + **PDF resume** attached.
4. `GET /` in a browser → the dashboard.
5. Set a near-term reminder and confirm delivery once it's due.

## The submit worker — you probably shouldn't deploy it

`worker/` can run as a second Fly app, but for personal use **don't**. Run it on your
laptop instead:

```bash
set -a; . ./.env; set +a          # BASE_URL, APPLY_API_TOKEN, WORKER_AGENT…
.venv/bin/python -m worker.run --once
```

It claims fill requests from prod over HTTPS, so your phone still triggers it and the
approve/cancel gate is unchanged — the only difference is whose computer drives the
browser. Reasons to prefer this:

- It's a **2GB** machine. Even scaled to zero that's the most expensive thing in the
  setup the moment it wakes.
- The acceptance test wants it **headful** (`WORKER_HEADLESS=false`) so you can watch
  it fill — which you can't do on Fly anyway. See `worker/LIVE_TEST.md`.
- Nothing else depends on it being reachable. It polls outward; nothing calls in.

Deploy it later, if and when you want fills to happen while your laptop is shut:

```bash
fly launch --no-deploy --name job-search-worker --copy-config -c worker/fly.toml
fly secrets set -a job-search-worker \
  BASE_URL=https://job-search-tool.fly.dev APPLY_API_TOKEN=<same as the main app>
fly deploy -c worker/fly.toml
```

## What this costs

One `shared-cpu-1x` / 512MB machine that never sleeps, plus a 1GB volume. That's the
whole bill, and it's on the order of a few dollars a month.

The sizing isn't padding — **256MB OOM-killed wide discovery**, and the machine can't
scale to zero because the reminder and discovery loops are in-process APScheduler jobs
(see `app/scheduler.py`). Sleeping means no discovery. If you want it cheaper than
this you're changing the architecture, not the config: move the schedule to an
external cron hitting an endpoint, and SQLite to a hosted DB.

Your Anthropic spend is a separate bill and likely the larger one. The levers there
are `AGENT_MODEL` (Haiku is ~5x cheaper than Opus), `AGENT_TOKEN_BUDGET`, and
`JOB_POLL_SECONDS` (fewer ticks, fewer scored postings).

## Updating

```bash
fly deploy        # redeploys; the /data volume (your SQLite DB) is preserved
fly logs          # tail logs
fly ssh console   # shell into the machine if needed
```

## Back up the volume, so this is never a rebuild again

The volume is the only copy of your tracked applications and knowledge store. Export
periodically:

```bash
fly ssh console -a job-search-tool \
  -C "sh -c 'cd /app && python -m scripts.export_user U07LVJVD4PL /data/brain.db'"
fly ssh sftp get -a job-search-tool /data/brain.db brain-$(date +%Y%m%d).db
```
