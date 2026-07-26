# Submit worker — live-test checklist

The worker (`worker/run.py`) drives a real browser. Its fill logic is now covered by
`tests/test_worker_fill.py` (headless Chromium against local fixtures — run that
first, it's ~60s), so this session is about the one thing fixtures can't prove:
that **real ATS DOMs match the shapes we imitate**. Budget ~45–60 min.

```bash
.venv/bin/python -m pytest tests/test_worker_fill.py -q   # do this before you start
```

**Safety:** the worker **never submits without your explicit approval** — it fills,
screenshots, and waits. So you can test fill + preview on real postings all day and
simply **Cancel** instead of Approve. You only ever submit forms you actually mean to.

**Approving from your phone:** you no longer need the web page. When the worker
reports a preview you get a Slack message listing what it filled and what it left
for you; reply **`approve`** to submit or **`cancel`** to stop. `in flight` lists
everything in progress. The `/apply` page still works exactly as before.

**Reading a failure:** the worker now logs one line per field —
`'Email Address' key=email text filled — rahil@…` / `'Gender' key=- eeo skipped —
demographic`. When something doesn't fill, that line says whether the label failed
to match, the identity was empty, or the click threw. Reproduce it by adding a
fixture to `tests/fixtures/forms/` rather than only tuning against the live site.

---

## 0. Setup (once)

```bash
cd ~/Documents/job-search-tool
.venv/bin/pip install -r worker/requirements.txt
.venv/bin/python -m playwright install chromium
```

Point the worker at **prod** (so it claims the same fill requests your phone creates)
and run it **headful** so you can watch:

```bash
BASE_URL=https://job-search-tool.fly.dev \
APPLY_API_TOKEN=<the same APPLY_API_TOKEN set as a Fly secret> \
WORKER_HEADLESS=false \
  .venv/bin/python -m worker.run --once
```

- `--once` = claim one job, fill it, wait for your approve/cancel, exit. Re-run for
  each test. Drop `--once` to leave it polling.
- The token must match the `APPLY_API_TOKEN` Fly secret, or `/worker/claim` 401s.
- Resume upload needs the base `.tex` files on the volume (`/data/resumes/swe.tex` +
  `aiml.tex`) — see handoff §5.4. Without them the resume just shows as skipped; the
  rest still works.

> Local-only variant: run `uvicorn app.main:app` and set `BASE_URL=http://localhost:8000`,
> but then you must create the fill request locally (open `/apply?user=<you>` on the
> same DB and hit "🤖 Auto-fill & submit"). Prod is simpler — your phone is the trigger.

---

## 1. The loop for each test form

1. On your **phone**, open `/apply`, find a staged posting, tap **🤖 Auto-fill & submit**.
   (Need one staged? `queue N` in Slack, or `queue top 3`.)
2. Run the worker command above. Watch the Chromium window fill the form.
3. The worker prints `filled N, awaiting approval` and posts the preview to your phone.
4. On your phone, check the preview: the **screenshot**, the **filled** chips, and the
   **"Left for you"** (skipped) list.
5. **Cancel** (for pure tuning) or **Approve** (only if you mean to apply).
6. Note what was wrong (below), fix `app/fieldmatch.py` / `submit_form`, redeploy the
   main app if you touched `fieldmatch`, re-test.

---

## 2. Coverage — hit at least one of each ATS

The fill logic forks by ATS DOM. Test all three; they fail differently.

| ATS | URL shape | Watch for |
|-----|-----------|-----------|
| **Greenhouse** | `boards.greenhouse.io/...` or `job-boards.greenhouse.io` | The friendliest: native `<input>`/`<select>`. Resume upload is a real `<input type=file>` (often hidden behind a styled button) — should attach. |
| **Lever** | `jobs.lever.co/...` | Native fields; "Additional information" is a `<textarea>` (essay path). Resume field labeled "Resume/CV". |
| **Ashby** | `jobs.ashbyhq.com/...` | The hard one: many dropdowns are **custom React** widgets, not `<select>` — expect these in skipped (known limitation). Yes/No may be radio groups or React buttons. |

And exercise each **field type** at least once across your test forms:
- text facts (name, email, phone, location, links)
- a native `<select>` dropdown (e.g. "How did you hear about us", country)
- a Yes/No question (work authorization, sponsorship)
- a free-text essay (`<textarea>` / "Why this company?")
- the **resume upload**
- the **submit button** (only on a form you actually approve)

---

## 3. What to check, and where the fix goes

### A field that should've filled landed in "skipped"
- **Label not recognized** → add/loosen a regex in `FIELD_RULES` in
  [`app/fieldmatch.py`](../app/fieldmatch.py). Add a case to `tests/test_fieldmatch.py`
  with the exact label so it's locked in. This fix **also improves the extension**
  (shared brain).
- **Dropdown matched the field but no option fit** → the value vs option mismatch is in
  `select_value` (e.g. you have `"Yes"` but options are `"Yes, I am authorized"`). It
  already does exact → substring; widen only if a real case needs it.
- **Custom React dropdown (Ashby)** → expected skip for now; note the posting so we can
  decide whether custom-widget support is worth building.

### A field filled with the *wrong* value
- Two rules matched and the wrong one won → `FIELD_RULES` is **order-sensitive**
  (first match wins; specific patterns sit above general ones). Reorder or tighten.
- Add the failing label to the tests as a regression guard.

### Resume didn't attach
- Check the worker log for `resume attach failed` / `resume fetch failed`.
- Field labeled something unusual → extend `_RESUME_LABEL` in `fieldmatch.py`
  (`is_resume_field`), add a test case.
- Multi-upload form where the resume input is unlabeled → the lone-input fallback won't
  fire (there's more than one). Note it; may need a positional heuristic.
- Empty fetch → base resume `.tex` likely missing on the volume (setup note above).

### An EEO/demographic field got filled
- It shouldn't — `_NEVER_FILL` blocks these. If one slips through, add the wording to
  `_NEVER_FILL` and a test. **This is the one to never get wrong.**

### Screenshot looks wrong on the phone
- Blank/tiny → the form was in an iframe or behind a consent wall; note the URL.
- Huge/slow → it's a full-page JPEG at quality 55; if a form is absurdly long, lower
  quality in `_screenshot()`.

### Submit didn't work (only seen if you Approve)
- `no submit button found` → add the form's selector/button text to the list in
  `submit_form()` in `worker/run.py`.
- Submitted but didn't register as applied → check `/worker/result` posted
  `status=submitted` and the posting got marked (it logs an application automatically).

---

## 4. Capture results

Keep a running note (paste into the next session) per form:

```
ATS / URL:
filled:   <count + anything notable>
skipped:  <list — the tuning targets>
resume:   attached? yes/no
screenshot: ok? yes/no
submit:   (only if approved) ok? yes/no
fix made: <fieldmatch regex / submit selector / none>
```

After a round of `fieldmatch` fixes: `.venv/bin/python -m pytest tests/test_fieldmatch.py -q`,
then redeploy the main app (`flyctl deploy -a job-search-tool`) so prod serves the
updated brain to both the worker and the extension.

---

## 5. Done when

- One real submit went all the way through on Greenhouse **and** Lever (the native-form
  ATSes), logged itself as Applied, and showed a faithful screenshot first.
- Ashby fills the native fields; remaining skips are only the known custom-React widgets.
- No EEO/demographic field ever auto-filled across any test.
