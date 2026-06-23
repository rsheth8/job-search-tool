# Submit worker (Phase 2)

A separate, on-demand service that **fills and — after you approve the preview —
submits** public application forms (Greenhouse / Lever / Ashby; no login needed).
It reuses `app/fieldmatch.py`, so it fills fields exactly like the browser
extension does, and it **never submits without your explicit approval**.

## Two fill engines

- **Hard-coded filler** (default) — `app/fieldmatch.py` rules map labels → values.
  Fast, free, deterministic; loses to iframes, multi-step flows, and label variety.
- **LLM browser agent** (`WORKER_AGENT=true`, `worker/agent.py`) — Claude looks at the
  page's interactive elements each step and picks one action (fill / choose / click /
  upload / scroll) until the form is filled, then hands off to your approval gate.
  Handles "Apply" buttons, multi-page forms, and odd fields the rules miss. Needs
  `ANTHROPIC_API_KEY`; model is `AGENT_MODEL` (default `claude-opus-4-8`, drop to
  `claude-haiku-4-5` for cost). It **never submits**, **never fills EEO fields**, and
  calls `blocked` on a login/captcha (→ handed off to the extension).

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

## Status / limitations (the honest part)

This is the one piece that **can't be unit-tested** — it drives a live browser
(the field-matching brain it calls, `app/fieldmatch.py`, *is* fully tested).
Known rough edges to tune against real forms:
- Submit-button detection is heuristic (`submit_form`); per-ATS selectors may need
  tightening.
- Resume upload targets `<input type="file">` whose label reads as a resume/CV, or
  the lone file input if there's exactly one. Multi-upload forms with an unlabeled
  resume field may still need a manual attach.
- Custom React dropdowns (some Ashby/Workday widgets) aren't native `<select>` and
  may not fill; the preview will list them as skipped.
- Holds one job open while awaiting approval (fine for personal volume).

Run it on a few real Greenhouse/Lever/Ashby applications with `WORKER_HEADLESS=false`
first, note what lands in "skipped", and tighten the rules in `app/fieldmatch.py`
(shared with the extension) and `submit_form`.

**Doing that live-test? Follow [`LIVE_TEST.md`](LIVE_TEST.md)** — a step-by-step
checklist (setup, per-ATS coverage, and exactly where each kind of fix goes).
