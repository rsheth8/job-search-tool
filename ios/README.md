# Apply — the iPhone job-application browser

A small native iOS app (SwiftUI + WKWebView). It's a **browser in your hand** that
knows your job profile: tap a match → it opens the real application form in-app →
hit **⚡ Autofill** to fill your identity + tailored answers → solve any captcha /
attach the resume yourself → submit on the site → **✓ I applied** logs it.

Why this and not a headless robot: an in-app browser is a *real* browser session
(your cookies, your IP, your taps), so logins, captchas, and anti-bot defenses are
just you using a browser — autofill rides on top. It's the robust, consistent path.

All the brains stay in the existing FastAPI backend — matches, identity, tailored
per-question answers, resume, application tracking. This app is a thin, fast mobile
face over those endpoints (`/apply/data`, `/apply/package`, `/apply/applied`).

## What's here

```
ios/
  project.yml        XcodeGen — the whole Xcode project, defined in text
  Apply/
    ApplyApp.swift     @main + tab shell
    Config.swift       backend URL / user / token (UserDefaults; sane defaults)
    Models.swift       Codable models for the API
    APIClient.swift    async networking to the backend
    Autofill.swift     the injected autofill engine (ported from extension/content.js)
    WebView.swift      WKWebView wrapper; injects the profile every page load
    QueueView.swift    your staged matches
    ApplyView.swift    the in-app apply browser (Autofill + I-applied controls)
    SettingsView.swift backend config
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

Open **Settings** in the app. Defaults already point at the live deploy
(`https://job-search-tool.fly.dev`) and your Slack user id (`U07LVJVD4PL`); set the
`APPLY_API_TOKEN` if your backend requires it.

## How autofill works

`Autofill.swift` injects two scripts on every page load (main frame **and** iframes):
your profile (`window.__APPLY`) at document start, and the matcher at document end.
The native **⚡ Autofill** button calls `window.__applyAutofill()`, which fills text
fields, native dropdowns, and Yes/No radios from your identity, fills free-text
questions from your tailored answers, **skips EEO/demographic fields**, and reports
how many it filled (and how many still need you). It's the same field-matching brain
as the desktop extension (`extension/content.js` / `app/fieldmatch.py`).

## The four tabs

- **Apply** — matches ready to apply to, plus the top matches you can stage with
  *Prepare application*. Each row says **why** it surfaced ("matches 'backend
  engineer' · python, go · remote · apply direct") and flags concerns. Paints from a
  local cache instantly, so a slow network never shows you an empty screen.
- **In flight** — what the submit worker is doing, and the **approval gate**: the
  filled fields, what was left for you, the worker's screenshot, and Approve /
  Cancel. Nothing is ever submitted without that tap.
- **About me** — how completely the assistant knows you (the lever on how much it
  can fill unattended), and the facts it draws on. Add projects, achievements, and
  reusable answers here; a saved answer is reused verbatim, with no model call.
- **Settings** — backend, user, token, and notifications.

## Autofill rules come from the backend

`app/fieldmatch.py` is the one source of truth for all three autofill surfaces (this
app, the desktop extension, the submit worker). The app fetches the rules from
`GET /apply/rules` and caches them; the copy bundled in `Autofill.swift` is only an
offline fallback, generated from the Python.

This matters because the hand-ported copy had **drifted**: it carried a narrower EEO
list than the backend, so the phone would fill marital status, religion, citizenship
status, and date of birth — fields the worker refuses. `tests/test_ios_autofill.py`
now runs this app's actual JavaScript engine against real form fixtures in headless
Chromium and proves it never fills those, on served *or* bundled rules. The fill
toast says "offline rules" when the bundled set ran, so staleness is visible.

If you edit the fallback rules by hand you've reintroduced the drift — regenerate
them from `app/fieldmatch.py` instead (a test fails if they diverge).

## Notifications

Turn them on in **Settings** (asked on demand, not at launch). You get told when new
matches land, and when a filled application is waiting on your approval; tapping
either lands on the tab that answers it.

Requires the backend to have APNs credentials (`PUSH_ENABLED` + `APNS_KEY_ID`,
`APNS_TEAM_ID`, `APNS_BUNDLE_ID`, `APNS_KEY_PATH`), which needs the paid Apple
Developer Program. Until then Settings tells you notifications won't arrive rather
than registering into a void. The `aps-environment` entitlement in `project.yml` is
`development`, which pairs with `APNS_USE_SANDBOX=true`; switch **both** for
TestFlight or you'll get `BadDeviceToken`.

## Known limitations

- **Resume = one tap.** iOS won't let an app inject a file into a web `<input
  type=file>` (security). The PDF is pre-downloaded when the form opens, so the
  Resume button opens the share sheet instantly — but you still pick it from Files
  yourself. That tap is the floor; it can't be removed.
- **Exotic custom dropdowns** may still not fill. ARIA comboboxes
  (`role=combobox`/`listbox`) do; widgets that expose neither role don't, and you
  set those by hand.
