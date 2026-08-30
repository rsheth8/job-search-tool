# Invite-only iOS beta

Closed TestFlight for you plus a handful of trusted testers. iOS + JSON APIs only.

The tester path: **Sign in with Apple → setup → matches → ⚡ Autofill → I applied.**

## Prod secrets (Fly)

`AUTH_FAIL_OPEN=false` is already in [`fly.toml`](../fly.toml). These must be
**secrets** (not git, not `.env.example`):

```bash
# Generate a token if you don't have one yet:
# python3 -c "import secrets; print(secrets.token_urlsafe(32))"

fly secrets set -a job-search-tool \
  APPLY_API_TOKEN=... \
  AUTH_ALLOW_DEV_LOGIN=false \
  AUTH_ALLOWED_EMAILS=you@example.com,friend@example.com \
  APPLE_CLIENT_IDS=com.rahil.jobpilot \
  SENTRY_DSN=https://...@...ingest.sentry.io/...

# First Apple login only — fold old dev rows into the new usr_…:
# fly secrets set -a job-search-tool AUTH_LEGACY_USER_ID=local
# Then clear it:
# fly secrets unset -a job-search-tool AUTH_LEGACY_USER_ID
```

Optional: `FEEDBACK_NOTIFY_USER` (your `usr_…` id) so **Send feedback** pings you
in chat. `JOB_ALERT_USER` (also `usr_…`) pins discovery digests to your account.

Push: `PUSH_ENABLED`, `APNS_*`, and **`APNS_USE_SANDBOX=false`** for TestFlight
(Release entitlements use the production APNs host).

Confirm: `curl -s https://job-search-tool.fly.dev/health | jq '{auth, beta, reminder_delivery, db_ok, status}'`
must show `fail_open: false`, `dev_login: false`, `invite_ready: true`, `db_ok: true`,
and `reminder_delivery: "app"`. If `invite_ready` is false, do not invite anyone.

## TestFlight

1. Apple Developer Program + App Store Connect app `JobPilot` (`com.rahil.jobpilot`).
2. `cd ios && xcodegen generate && open JobPilot.xcodeproj`
3. Archive a **Release** build (uses `JobPilotRelease.entitlements` →
   `aps-environment: production`).
4. Upload → Internal testers (fits a handful of friends).
5. Add each Apple email to `AUTH_ALLOWED_EMAILS` **before** they tap Sign in.
   If they use Hide My Email, add the `privaterelay.appleid.com` address.

## Invite one friend, then the group

1. Dogfood the TestFlight build yourself against Fly. Confirm your pipeline
   did not leak onto a throwaway Apple/sandbox account.
2. Apply to one real Greenhouse/Lever/Ashby form via Autofill (résumé is still
   a manual attach on the phone).
3. Add one friend's email → TestFlight invite → watch `/feedback` and Sentry
   for a day.
4. Then invite the rest.

```bash
# Read recent feedback from the volume:
fly ssh console -a job-search-tool -C "cd /app && python -c \"
from app.feedback import list_recent
from pprint import pprint
pprint(list_recent(20))
\""
```

## Tester brief (also in Settings → For testers)

1. Sign in with Apple (invite-only). Hide My Email is fine if that relay
   address is on the allowlist.
2. Finish setup: roles + locations, identity, one project. Matches start
   searching as soon as you save roles.
3. Pull to refresh to search again if the list is empty.
4. Prepare → Autofill on Greenhouse, Lever, or Ashby. Attach the résumé
   yourself (Files). You always tap Submit.
5. Workday and LinkedIn Easy Apply are out of scope.
6. Use **Send feedback** when something's off. That report includes the last
   request id. Settings → Diagnostics copies a fuller dump if they email you.

## When something breaks

Every API response has `X-Request-Id`. The iOS app stores the last one and
attaches it to **Send feedback** as `context` (app version, path, status). The
snippet above prints `body` and `context`.

Unhandled server errors return JSON `{detail, code, request_id}` — never a
traceback. `/health` includes `db_ok`; `status` is `degraded` if SQLite is down
(Fly still gets HTTP 200 so a lock doesn't bounce the machine).

```bash
# Recent 5xx in logs (request ids):
fly logs -a job-search-tool | grep -E 'rid=|unhandled'
```

One warm machine, SQLite on `/data`. Before inviting people:

```bash
fly volumes snapshots create data -a job-search-tool
```
