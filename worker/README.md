# Submit worker (Phase 2)

A separate, on-demand service that **fills and — after you approve the preview —
submits** public application forms (Greenhouse / Lever / Ashby; no login needed).
It reuses `app/fieldmatch.py`, so it fills fields exactly like the browser
extension does, and it **never submits without your explicit approval**.

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
Drop `--once` to run it as a continuous poller. Set `headless=False` in
`worker/run.py` while testing so you can watch it fill.

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
  questions, and free-text questions (using your pre-drafted answers).
- **Leaves for you:** file uploads, consent checkboxes, EEO/demographic questions,
  and anything it can't confidently match — all listed in the preview.

## Status / limitations (the honest part)

This is the one piece that **can't be unit-tested** — it drives a live browser.
Known rough edges to tune against real forms:
- Submit-button detection is heuristic (`submit_form`); per-ATS selectors may need
  tightening.
- File (resume) upload isn't wired yet — upload your resume manually for now.
- Custom React dropdowns (some Ashby/Workday widgets) aren't native `<select>` and
  may not fill; the preview will list them as skipped.
- Holds one job open while awaiting approval (fine for personal volume).

Run it on a few real Greenhouse/Lever/Ashby applications with `headless=False`
first, note what lands in "skipped", and tighten the rules in `app/fieldmatch.py`
(shared with the extension) and `submit_form`.
