# Submit worker — live-test checklist

The worker (`worker/run.py`) drives a real browser. Fixture tests prove the engines
against **imitations** of ATS shapes. This session is the one thing fixtures cannot
prove: that **real Greenhouse / Lever / Ashby DOMs match those shapes**. Budget
~45–60 min. CI never hits live ATS — you have to.

**Do this first (must be green):**

```bash
# local, with Chromium installed
.venv/bin/python -m pytest tests/test_worker_fill.py tests/test_ios_autofill.py \
  tests/test_extension_autofill.py tests/test_rules_parity.py -q
```

GitHub Actions (`.github/workflows/pytest.yml`) runs that same suite — including
headless Chromium — on every PR, and **blocks Fly deploy** on `main` until it
passes. A green CI check is not a live-ATS pass.

**Safety:** the worker **never submits without your explicit approval** — it fills,
screenshots, and waits. Test fill + preview all day and **Cancel**. Only Approve
forms you actually mean to send.

**Approving from your phone:** when the worker reports a preview you get a Slack
message listing what it filled and what it left for you; reply **`approve`** or
**`cancel`**. `in flight` lists everything in progress. `/apply` still works.

**Reading a failure:** the worker logs one line per field —
`'Email Address' key=email text filled — rahil@…` / `'Gender' key=- eeo skipped —
demographic`. If something doesn't fill, that line says whether the label failed
to match, the identity was empty, or the click threw. Reproduce it by adding a
fixture under `tests/fixtures/forms/` rather than only tuning against the live site.

---

## 0. Setup (once)

```bash
# repo root
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
6. Note what was wrong (below), add a fixture + test, fix `app/fieldmatch.py` /
   `worker/run.py` / the JS engines, then re-run the Chromium suite before redeploy.

---

## 2. Coverage — hit at least one of each ATS

The fill logic forks by ATS DOM. Test all three; they fail differently.

| ATS | URL shape | Watch for |
|-----|-----------|-----------|
| **Greenhouse** | `boards.greenhouse.io/...` or `job-boards.greenhouse.io` | Friendliest: native `<input>`/`<select>`. Resume is often a hidden `<input type=file>` behind a styled button — should attach. Newer boards use react-select; if a dropdown stays skipped, capture it as a fixture. |
| **Lever** | `jobs.lever.co/...` | Native fields; "Additional information" is a `<textarea>` (essay path). Resume field labeled "Resume/CV". Reveal-then-form is covered by `lever_apply_reveal.html` locally. |
| **Ashby** | `jobs.ashbyhq.com/...` | The hard one: many dropdowns are **custom React** widgets, not `<select>`. Yes/No may be radios **or** big `<button>` pairs (`ashby_yesno_buttons.html` — iOS fills these; extension bulk fill does not; worker may still skip custom widgets). |

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
  with the exact label so it's locked in. This fix **also improves the extension
  and iOS** (shared brain).
- **Dropdown matched the field but no option fit** → the value vs option mismatch is in
  `select_value` (e.g. you have `"Yes"` but options are `"Yes, I am authorized"`). It
  already does exact → substring; widen only if a real case needs it.
- **Custom React dropdown (Ashby / Greenhouse react-select)** → expected skip on the
  worker until that widget is ported. Note the posting URL; first add a fixture, then
  the engine fix.

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
- Hard-blocked fields (`orientation`, hispanic/latino, gender identity, DOB, …)
  must stay empty. Optional demographics (gender/race/veteran/disability) fill **only**
  when identity has a value. If a hard-blocked one slips through, add the wording to
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
surface:  worker (this checklist) / iOS / extension
filled:   <count + anything notable>
skipped:  <list — the tuning targets>
resume:   attached? yes/no
screenshot: ok? yes/no
eeo:      hard-blocked empty? optional only if identity set?
submit:   (only if approved) ok? yes/no
fixture:  added tests/fixtures/forms/<name>.html ? yes/no
fix made: <fieldmatch regex / worker selector / JS engine / none>
```

After a round of fixes:

```bash
.venv/bin/python -m pytest tests/test_fieldmatch.py tests/test_worker_fill.py \
  tests/test_ios_autofill.py tests/test_extension_autofill.py tests/test_rules_parity.py -q
```

Then redeploy the main app (`flyctl deploy -a job-search-tool`) so prod serves the
updated brain to the worker, the extension, and the phone. Deploy now waits on CI.

---

## 5. Done when

- One real submit went all the way through on Greenhouse **and** Lever (the native-form
  ATSes), logged itself as Applied, and showed a faithful screenshot first.
- Ashby fills native fields; remaining skips are only known custom-React widgets.
- No hard-blocked EEO/demographic field ever auto-filled across any test.
- Any live miss has a fixture + a failing-then-passing Chromium test before you
  consider it fixed.
