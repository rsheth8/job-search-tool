# Submit worker (Phase 2)

A separate, on-demand service that **fills and — after you approve the preview —
submits** public application forms (Greenhouse / Lever / Ashby; no login needed).
It reuses `app/fieldmatch.py`, so it fills fields exactly like the browser
extension does, and it **never submits without your explicit approval**.

## Two fill engines

- **Hard-coded filler** (default) — `app/fieldmatch.py` rules map labels → values.
  Fast, free, deterministic; loses to iframes, multi-step flows, and label variety.
- **Hybrid agent** (`WORKER_AGENT=true`, `worker/agent.py`) — **code first, LLM only
  when needed.** Each step a deterministic pass (`auto_fill`, the `fieldmatch` rules)
  fills everything it can for free — identity text fields, native dropdowns, the
  resume upload — and the LLM is consulted **only** when that pass makes no more
  progress, to handle what code can't: clicking through "Apply"/"Next" steps, writing
  the free-text answers, picking Yes/No radios, and ambiguous fields. So a clean
  one-page form costs ~1–2 LLM calls (the essay + the handoff), not one per field.
  Needs `ANTHROPIC_API_KEY`; model is `AGENT_MODEL` (default `claude-opus-4-8`, drop
  to `claude-haiku-4-5` for cost). It **never submits**, **never fills EEO fields**,
  and calls `blocked` on a login/captcha (→ handed off to the extension).

## How it fits

```
Phone (/apply)  ──"🤖 Auto-fill & submit"──►  main app  ──fill request──►  worker
      ▲                                                                      │
      │   approve the preview                  fills public form, screenshots │
      └──────────────────────────────  preview to phone  ◄───────────────────┘
                          (you tap Approve → worker submits)
```

The state machine lives in `app/fill_requests.py`:
`pending → filling → preview → approved → submitting → submitted | failed`.

## Run locally (test against a real form, watching)

```bash
pip install -r worker/requirements.txt
python -m playwright install chromium

BASE_URL=http://localhost:8000 APPLY_API_TOKEN=<your token> \
  python -m worker.run --once     # process one job and exit
```
Drop `--once` to run it as a continuous poller. Set `WORKER_HEADLESS=false` while
testing so you can watch it fill.

## Deploy as a separate Fly app

```bash
fly launch --no-deploy --name job-search-worker --copy-config -c worker/fly.toml
fly secrets set BASE_URL=https://job-search-tool.fly.dev APPLY_API_TOKEN=<token> -a job-search-worker
fly deploy -c worker/fly.toml -a job-search-worker
```
It's a ~2GB box (Chromium) separate from the main 512MB app, and scales to zero
when idle (set `min_machines_running = 1` for instant fills).

## What it fills / doesn't

- **Fills:** text facts (name, email, location, links, …), dropdowns, Yes/No
  questions, free-text questions (using your pre-drafted answers), and the
  **tailored resume** — attached to the resume/CV upload field (`fieldmatch.is_resume_field`
  picks it, never a cover-letter upload).
- **Leaves for you:** cover-letter / other file uploads, consent checkboxes,
  EEO/demographic questions, and anything it can't confidently match — all listed
  in the preview.

The preview sent to your phone includes a **full-page screenshot** of the filled
form (`screenshot_url`, a JPEG data URL), so you approve against the actual form,
not just a field list.

## Tests

The fill logic **is** tested — `tests/test_worker_fill.py` drives real headless
Chromium against hand-written fixtures in `tests/fixtures/forms/` served from a
local HTTP server. No network, no credentials, no live ATS site. The module skips
cleanly where Playwright/Chromium isn't installed, so a browserless CI still passes.

Fixtures reproduce the shapes real forms take: a plain Greenhouse-style form, a
Lever description page that reveals its form on click, a form inside an `<iframe>`,
an ARIA combobox + Yes/No radios + essay, a late-rendering SPA, and an EEO section.

Two invariants have tests that fail if they ever regress:

1. **`fill_form` never submits** — asserted across every fixture.
2. **EEO/demographic fields are never filled** — on every control type, including
   the back door where a long demographic question ("Are you Hispanic or Latino?")
   looked enough like an essay to get a drafted answer written into it.

```bash
.venv/bin/python -m pytest tests/test_worker_fill.py -q      # ~60s
```

## Status / limitations (the honest part)

What the fixtures **can't** prove is that real ATS DOMs match the shapes they
imitate. A live headful run is still the acceptance test — it's just no longer the
*first* place bugs are found. Remaining rough edges:

- Submit-button detection is heuristic (`submit_form`) — now searches every frame
  and refuses reveal/navigation buttons, but per-ATS selectors may need tightening.
- Resume upload targets `<input type="file">` whose label reads as a resume/CV, or
  the lone file input if there's exactly one. Multi-upload forms with an unlabeled
  resume field may still need a manual attach.
- Custom dropdowns now fill via click-open → click-option (ARIA
  `role=combobox`/`listbox`). Widgets that render neither role still get skipped
  and are listed in the preview.
- Holds one job open while awaiting approval (fine for personal volume).

Run it on a few real Greenhouse/Lever/Ashby applications with `WORKER_HEADLESS=false`,
watch the per-field log (`label → key → action → result`), and tighten the rules in
`app/fieldmatch.py` (shared with the extension) or add a fixture reproducing whatever
broke.

**Doing that live-test? Follow [`LIVE_TEST.md`](LIVE_TEST.md)** — a step-by-step
checklist (setup, per-ATS coverage, and exactly where each kind of fix goes).
