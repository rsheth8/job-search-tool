# Deploying to Fly.io

Always-on hosting keeps **reminders and discovery ticking** (in-process
APScheduler) and gives the iOS app a stable API URL. Invite-only beta runbook:
[`BETA.md`](BETA.md).

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
#    See BETA.md for the invite-only iOS set (APPLY_API_TOKEN, AUTH_ALLOWED_EMAILS,
#    SENTRY_DSN, Apple). AUTH_FAIL_OPEN=false is already in fly.toml.
fly secrets set \
  ANTHROPIC_API_KEY=sk-ant-... \
  APPLY_API_TOKEN=... \
  AUTH_ALLOWED_EMAILS=you@example.com
```

Only `ANTHROPIC_API_KEY` and the two `SLACK_*` values are needed for the core
experience; `APOLLO_API_KEY` is optional (recruiter discovery). `JOB_ALERT_USER`
is your Slack user id — required for job-discovery digests (find it in Slack →
profile → ⋯ → Copy member ID).

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
fly ssh sftp put -a job-search-tool resumes/swe.tex /data/resumes/swe.tex
fly ssh sftp put -a job-search-tool resumes/aiml.tex /data/resumes/aiml.tex
fly ssh console -a job-search-tool -C "ls -la /data/resumes/"
```

Do **not** use `scp root@*.fly.dev` — Fly closes that connection. Use `fly ssh sftp put` instead.

Tailored outputs cache under `/data/resumes/tailored/`. Tectonic is in the Docker image.

## Deploy

```bash
fly deploy
fly status                       # confirm one machine is running
curl https://<app>.fly.dev/health   # router, scheduler, auth.fail_open: false
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

1. TestFlight: Sign in with Apple → finish setup → matches appear.
2. `GET /health` → `auth.fail_open` is false, `dev_login` is false.
3. Unauthenticated `GET /apply/data` → 401.

## Updating

```bash
fly deploy        # redeploys; the /data volume (your SQLite DB) is preserved
fly logs          # tail logs
fly ssh console   # shell into the machine if needed
```
