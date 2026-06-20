# Job Apply Autofill — browser extension

An inline autofill assistant for job applications, powered by your
`job-search-tool` server. As you focus a field on an application page it offers a
suggestion — your name/email/links/work-authorization from your profile, or a
Haiku-drafted answer for free-text questions. **You click to accept; nothing is
ever filled or submitted automatically.**

This is the client for the `/apply/*` API added to the server (identity +
question-answer endpoints, CORS, optional token).

## Install (desktop Chrome / Edge / Brave)

1. Go to `chrome://extensions`, enable **Developer mode** (top-right).
2. **Load unpacked** → select this `extension/` folder.
3. Click the extension's **Open settings** (or the puzzle-piece → Job Apply
   Autofill → Options) and fill in:
   - **Server URL** — e.g. `https://job-search-tool.fly.dev`
   - **User id** — the same id you use in the app (e.g. `local`)
   - **API token** — only if you set `APPLY_API_TOKEN` on the server
   - **Your details** — name, email, links, work authorization, etc. Saving
     syncs these to the server (`POST /apply/identity`).
4. Open a job application on a supported site and click into a field — a blue
   chip appears with the suggestion. Click it to fill.

## iPhone (Safari)

Chrome on iOS can't run extensions, but **Safari on iOS 15+ can**. The same code
can be packaged as a Safari Web Extension via Xcode's converter
(`xcrun safari-web-extension-converter extension/`) and side-loaded. Filling a
long application on a phone is still clunky — the `/apply` web queue is usually the
better phone experience.

## Supported sites

Greenhouse, Lever, Ashby, Workday, iCIMS, SmartRecruiters, Workable, BambooHR
(see `manifest.json` `content_scripts.matches`). Add hosts there as needed.

## How it decides what to fill

`content.js` reads each field's label (`<label>`, `aria-label`, placeholder,
`name`/`id`) and matches it against the rules in `RULES`. Identity matches fill
from the cached `/apply/identity` map; long/`?`-ending fields and textareas get the
**✨ Draft answer** action, which calls `/apply/answer` with the question (and the
configured posting id for JD context). Values are written with native setters +
`input`/`change` events so React-based forms register them.

## Privacy / safety

- Your details live in your server + the browser's `chrome.storage.sync`; the
  extension holds **no ATS passwords** and rides the sessions you're already
  logged into.
- Demographic / EEO questions (race, gender, disability, veteran status) are
  **never** matched or filled — those stay a manual choice.
- There is no submit path. The extension only ever populates fields you focus.
