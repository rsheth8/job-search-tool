# Apply — the iPhone job-application browser

A small native iOS app (SwiftUI + WKWebView). It's a **browser in your hand** that
knows your job profile: tap a match → it opens the real application form in-app →
hit **⚡ Autofill** to fill your identity + tailored answers → attach the résumé
yourself → **Submit** on the site → **✓ I applied** logs it.

Why this and not a headless robot: an in-app browser is a *real* browser session
(your cookies, your IP, your taps), so logins, captchas, and anti-bot defenses are
just you using a browser — autofill rides on top. **Autofill is the only autofill
surface**; there is no desktop extension or auto-submit worker.

All the brains stay in the existing FastAPI backend — matches, identity, tailored
per-question answers, resume, application tracking. This app is a thin, fast mobile
face over those endpoints (`/apply/data`, `/apply/package`, `/apply/applied`).

## What's here

```
ios/
  project.yml        XcodeGen — the whole Xcode project, defined in text
  Apply/
    ApplyApp.swift     @main + tab shell
    Config.swift       backend URL / token (UserDefaults; sane defaults)
    Models.swift       Codable models for the API
    APIClient.swift    async networking to the backend
    Autofill.swift     injected autofill engine (JS matcher)
    WebView.swift      WKWebView wrapper; injects the profile every page load
    QueueView.swift    staged matches (Apply tab)
    ApplyView.swift    in-app apply browser (Autofill + I-applied controls)
    KnowledgeView.swift  About tab — projects, achievements, coverage audit
    ChatView.swift     in-app assistant
    SettingsView.swift account, feedback, tester brief
```

## Build & run on your iPhone

You need a Mac with **Xcode** and an **Apple ID** (free works for dev).

```bash
brew install xcodegen
cd ios
xcodegen generate        # writes Apply.xcodeproj from project.yml
open Apply.xcodeproj
```

In Xcode:
1. Select the **Apply** target → **Signing & Capabilities** → set your **Team**
   (your Apple ID; Xcode auto-manages the cert). Change the bundle id if it clashes.
2. Plug in your iPhone, select it as the run destination, press **⌘R**.
3. First run: on the phone, **Settings → General → VPN & Device Management** → trust
   your developer cert.

> No XcodeGen? Create a new **iOS App (SwiftUI)** in Xcode named `Apply`, delete its
> stub `ContentView`/`App`, drag the files in `Apply/` into the project, and in the
> target's Info add `NSAppTransportSecurity → NSAllowsArbitraryLoadsInWebContent =
> YES` (so the browser can load any job site).

## Stable installs / pushing updates — TestFlight

Free signing re-installs expire after 7 days. For a stable install you keep, plus
the ability to push updates without a cable, use **TestFlight** (needs the **$99/yr
Apple Developer Program**):

1. In Xcode: **Product → Archive** → **Distribute App → App Store Connect → Upload**.
2. In App Store Connect, add the build to TestFlight and add yourself as a tester.
3. Install **TestFlight** on your iPhone; new uploads arrive there.

## Configure

Open **Settings** in the app after Sign in with Apple. Defaults point at the live
deploy (`https://job-search-tool.fly.dev`). Base URL / API token are under
**Advanced** — testers should not re-point the backend.

TestFlight **Release** archives use `ApplyRelease.entitlements`
(`aps-environment: production`). Set `APNS_USE_SANDBOX=false` on Fly to match.
Debug builds keep the sandbox entitlement.

Invite-only: the backend allowlist is `AUTH_ALLOWED_EMAILS`. Tester brief is
in Settings → For testers and [`deploy/BETA.md`](../deploy/BETA.md).

## How autofill works

`Autofill.swift` injects two scripts on every page load (main frame **and** iframes):
your profile (`window.__APPLY`) at document start, and the matcher at document end.
The native **Fill** button calls `window.__applyFillOrPause()`, which fills text
fields, native dropdowns, react-select comboboxes, and Yes/No button pairs from your
identity, fills free-text questions from your tailored answers, **skips EEO/demographic
fields**, and reports how many it filled (and how many still need you). Login walls
and CAPTCHAs pause instead of filling. Rules match `app/fieldmatch.py` (served from
`GET /apply/rules`). Sign-in cookies live in the shared `WKWebsiteDataStore`.

## The four tabs

- **Apply** — matches ready to apply to, plus top matches you can stage with
  *Prepare application*. Each row says **why** it surfaced and flags concerns. Paints
  from a local cache instantly, so a slow network never shows an empty screen.
- **About** — how completely the assistant knows you (coverage audit), plus projects,
  achievements, and reusable answers. A saved answer is reused verbatim with no model call.
- **Chat** — the in-app assistant (reminders, CRM, job questions). Same brain as the
  CLI; digests and reminders also land here.
- **Settings** — account, Send feedback, tester brief, Diagnostics (copy last
  request id), Advanced (backend URL). Send feedback attaches app version and
  the last failed request so you can match it to server logs.

## Sign in with Apple

The app gates on an account. In Xcode:

1. Signing & Capabilities → **+ Capability → Sign in with Apple**
   (also declared in `project.yml` / `Apply.entitlements`).
2. Backend needs `APPLE_CLIENT_IDS=com.rahil.apply` (and your Team set in Xcode).
3. To fold old dev-keyed data into the new account on first login, set
   `AUTH_LEGACY_USER_ID=local` (or your old id) on the server once, then clear it.

In-app Chat tab talks to `POST /chat` (Bearer session).
Apple JS button; otherwise use the iOS app).

## Autofill rules come from the backend

`app/fieldmatch.py` is the source of truth. The app fetches rules from
`GET /apply/rules` and caches them; the copy bundled in `Autofill.swift` is only an
offline fallback.

The bundled fallback had **drifted** in the past (narrower EEO list). `tests/test_ios_autofill.py`
runs this app's JavaScript engine against form fixtures and proves it never fills
blocked demographics. The fill toast says "offline rules" when the bundled set ran.

If you edit fallback rules by hand you reintroduce drift — regenerate from
`app/fieldmatch.py` instead (a test fails if they diverge).

## Notifications

Turn them on in **Settings** (asked on demand, not at launch). You get told when new
matches land; tapping opens the Apply tab.

Requires the backend to have APNs credentials (`PUSH_ENABLED` + `APNS_KEY_ID`,
`APNS_TEAM_ID`, `APNS_BUNDLE_ID`, `APNS_KEY_PATH`), which needs the paid Apple
Developer Program. Until then Settings tells you notifications won't arrive rather
than registering into a void. The `aps-environment` entitlement in `project.yml` is
`development`, which pairs with `APNS_USE_SANDBOX=true`; switch **both** for
TestFlight or you'll get `BadDeviceToken`.

## Known limitations

- **Résumé and cover letter = you attach.** iOS won't let an app inject a file into
  a web `<input type=file>` (security). The résumé PDF is pre-downloaded when
  the form opens (`GET /apply/resume`). Cover letter is optional and built when
  you pick it from the documents menu (`GET /apply/cover`) — not on every
  Preflight. Both open the share sheet; you still pick the file from Files
  yourself. That tap is the floor; it can't be removed.
- **You always Submit.** Autofill never clicks the final submit button.
- **Exotic custom dropdowns** may still not fill. ARIA comboboxes
  (`role=combobox`/`listbox`) do; widgets that expose neither role don't, and you
  set those by hand.
- **Workday widgets / LinkedIn Easy Apply** still need you (login-gated or a
  different UX). Public HTML forms, Workable, and SmartRecruiters get the same
  Fill pass; login and CAPTCHA pause with a banner. Sign-in cookies persist
  across applications.
