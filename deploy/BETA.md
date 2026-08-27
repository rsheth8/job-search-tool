# Invite-only iOS beta

Closed TestFlight for you plus a handful of trusted testers. Slack and the
web dashboards are owner tools, not the tester surface.

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
  APPLE_CLIENT_IDS=com.rahil.apply \
  SENTRY_DSN=https://...@...ingest.sentry.io/...

# First Apple login only — fold your old Slack/phone rows into the new usr_…:
# fly secrets set -a job-search-tool AUTH_LEGACY_USER_ID=U…
# Then clear it:
# fly secrets unset -a job-search-tool AUTH_LEGACY_USER_ID
```

Optional: `FEEDBACK_NOTIFY_USER` (your `usr_…` or Slack id) so Send feedback
pings you. Push: `PUSH_ENABLED`, `APNS_*`, and **`APNS_USE_SANDBOX=false`**
for TestFlight (Release entitlements use the production APNs host).

Worker token on `job-search-worker` must match `APPLY_API_TOKEN`. Auto-submit
stays **off** (`APPLY_AUTOSUBMIT_ENABLED=false`) so testers cannot fire a real
Playwright submit by accident.

Confirm: `curl -s https://job-search-tool.fly.dev/health | jq .auth`
should show `"fail_open": false`, `"dev_login": false`, `"autosubmit": false`.

## TestFlight

1. Apple Developer Program + App Store Connect app `Apply` (`com.rahil.apply`).
2. `cd ios && xcodegen generate && open Apply.xcodeproj`
3. Archive a **Release** build (uses `ApplyRelease.entitlements` →
   `aps-environment: production`).
4. Upload → Internal testers (fits a handful of friends).
5. Add each Apple email to `AUTH_ALLOWED_EMAILS` **before** they tap Sign in.

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

1. Sign in with Apple (invite-only).
2. Finish setup: roles + locations, identity, one project.
3. Wait for matches / pull to refresh.
4. Prepare → Autofill on Greenhouse, Lever, or Ashby. Attach the résumé yourself.
5. Workday and LinkedIn Easy Apply are out of scope.
6. Use **Send feedback** when something's off.

## Volume backup

One warm machine, SQLite on `/data`. Before inviting people:

```bash
fly volumes snapshots create data -a job-search-tool
```
