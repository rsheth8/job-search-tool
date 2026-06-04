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
  APOLLO_API_KEY=...
```

Only `ANTHROPIC_API_KEY` and the two `SLACK_*` values are needed for the core
experience; `APOLLO_API_KEY` is optional (recruiter discovery).

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
3. `GET /` in a browser → the dashboard.
4. Set a near-term reminder and confirm delivery once it's due (the scheduler
   ticks on the warm machine).

## Updating

```bash
fly deploy        # redeploys; the /data volume (your SQLite DB) is preserved
fly logs          # tail logs
fly ssh console   # shell into the machine if needed
```
