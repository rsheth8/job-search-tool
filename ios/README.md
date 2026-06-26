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

## Known limitations / next steps

- **Resume = one tap.** iOS won't let an app inject a file into a web `<input
  type=file>` (security). You tap the upload and pick the PDF from Files. *Next:* a
  "Save tailored resume to Files" button that pulls `GET /apply/resume`.
- **Custom React dropdowns** (some Ashby/Workday widgets) aren't native `<select>`
  and may not fill — same limitation as the extension; you set those by hand.
- No push notifications yet — open the app and pull to refresh. *Next:* a "new
  matches" notification that deep-links straight into the apply browser.
