# Overnight build — what landed

Branch **`worker-robust-fill`**, 5 commits, nothing pushed. Start with `./verify.sh`.

**Suite: 531 → 699 passed, 15 skipped.** (The doc said 496; the real baseline was 531.)
`./verify.sh` green, app imports clean.

All three priorities completed in order, each fully tested before the next started.

---

## Priority 1 — autonomous end-to-end, phone + computer

### 1a. The worker is no longer untestable

Every doc repeated that `worker/run.py` couldn't be tested because it drives a live
browser. That was only true of *live ATS sites*. `tests/fixtures/forms/` now holds
hand-written pages reproducing the shapes real forms take, served from a local HTTP
server and driven with the same Chromium the worker uses — no network, no
credentials, no live site. `tests/test_worker_fill.py`, 17 tests, ~60s.

Fixtures: `greenhouse_basic`, `lever_apply_reveal` (form appears on click),
`ashby_iframe` + inner (form in an `<iframe>`), `custom_dropdown` (ARIA combobox +
Yes/No radios + essay), `spa_late_form` (paints 1.2s late), `eeo_present`.

**Two invariants now have tests that fail if they regress:** `fill_form` never
submits (asserted on every fixture), and EEO fields are never filled (every control
type).

**Five real bugs the fixtures caught** — all of these were live:

| Bug | Effect |
|---|---|
| `is_essay_label` accepted EEO questions | "Are you Hispanic or Latino?" has no identity key, is >40 chars, and question-shaped — so it got a **drafted answer written into it**. An EEO field filled through the back door. |
| Matching ran on `label + name + id` concatenated | A bare `Name` field arrived as `"Name _systemfield_name"` and matched nothing. Ashby/Greenhouse name fields silently skipped. |
| `submit_form` searched only the top frame | An embedded form's submit button lives in the iframe → **an approved application never went out**. |
| Radio option text included `name`/`id` noise | Yes/No matching worked by substring luck. |
| Stale `data-jaf-opt` tags | The second combobox on a page timed out. |

Hardening: ARIA comboboxes and Yes/No radio groups now fill (previously skipped); a
settle pass re-reads two-stage React renders; a short reveal-probe stops description
pages burning the full wait budget (suite 219s → 64s); structured per-field logging
(`label → key → action → result + reason`) so a live run is diagnosable.

`fieldmatch` gained `is_eeo()` and `option_for()` as shared decision points;
`agent.py`'s private `_is_eeo` now delegates so all three fill paths agree.

### 1b/1c. Approve from Slack + in-flight view

The approval gate lived only on `/apply`, and the worker's preview **notified
nobody** — requests sat at `preview` until you happened to open the page.

Now: worker fills → app messages you what it filled, what it left for you → reply
`approve` or `cancel` → worker submits. Alert to submit without leaving Slack.
`in flight` lists everything in progress; the dashboard grew a **🤖 In flight**
section built from the same rows, so phone and computer can't disagree.

A bare `approve` with nothing at the gate explains *why* ("still filling", "already
submitted") instead of a flat "nothing is waiting". Notification is fail-open: a
messaging error still records the preview, since `/apply` is the fallback.

The gate itself is unchanged and tested — approving early (still filling) or as a
different user both leave the request untouched.

---

## Priority 2 — it knows you

`app/knowledge.py`: projects, achievements, strengths, preferences, and reusable
answers. Two payoffs — `knowledge_block()` grounds the drafting prompt so answers
cite real work, and **a saved answer matching the question is returned verbatim: no
model call, no cost, no variance.**

Question matching scores how much of the *saved* question the asked one covers, so
"Why do you want to work here?" still matches "…work at Acme?". It requires two
overlapping content words — one shared word is coincidence, not the same question.

Teach it from Slack: `remember project: I built …`, `remember I cut latency 40%`
(category inferred), `remember answer to "Why us?": …`. Inspect with
`what do you know about me`, which also reports the coverage audit.

Coverage audit (`knowledge.audit`) reports which identity fields are missing — the
lever on how much autofill can do unattended. Surfaced in Slack and as a dashboard
section that hides itself once nothing is missing.

Field coverage broadened: `Name (First)`, `Contact number`, `Where do you live?`,
`Homepage`, `Notice period`, `Year of graduation`, `How many years…`.
`Current salary` deliberately still matches nothing — we store a desired figure.

EEO guard widened: national origin, self-identification, EEO/equal opportunity,
protected class, LGBTQ, marital status, religion, citizenship status, date of birth.

---

## Priority 3 — real jobs, accurate, and it says why

**Accuracy.** Closed reqs ("this position has been filled", "no longer accepting
applications") are now dropped — and this is the **one rule that overrides
first-party trust**: a Greenhouse board is trustworthy about who's hiring, but a
closed req on it still burns a real application. Also: commission-only /
pay-to-play listings; staleness now parses weeks and months (it only understood
"N days ago", so "2 months ago" read as fresh); more placeholder employers ("Our
Client", "Fortune 500", recruiting agencies) and spam titles.

**Dedupe.** The same job arrives from the ATS, an RSS feed, and the aggregator at
once; source-level dedupe couldn't see it (different external ids) so it appeared
three times. `quality.dedupe` collapses them and keeps the first-party copy so you
apply direct. Wired into `discovery.tick`.

It merges **only across sources** — two reqs on the same board with the same title
are two real openings on different teams. My first version merged those; the
existing discovery test had exactly that shape and caught it.

**Transparency.** `app/fit.py` turns signals already computed into words:
`87% · matches "backend engineer" · python, go · remote · apply direct`, plus
concerns (`⚠️ title doesn't match your target roles`). Heuristic and free — every
reason is checkable against the posting, so it can't invent one, and it works with
no API key. Shown on the review card.

---

## ⚠️ Needs you — and why it can't be faked

1. **Mirror `fieldmatch` into `extension/content.js`.** The Python `RULES` gained
   label variants and a much wider EEO never-fill list; the extension's copy has
   drifted behind. I deliberately didn't edit it — the two are kept in lockstep by
   hand and I'd be changing autofill behavior in a surface with no tests. **Until
   this is done the extension will still autofill some EEO fields the worker now
   refuses** (marital status, religion, citizenship, DOB). Worth doing first.

2. **Live headful worker test** (`worker/LIVE_TEST.md`, now ~45 min instead of
   90). Fixtures prove the logic handles those *shapes*; they can't prove a real
   Greenhouse DOM matches them. Run `WORKER_HEADLESS=false`, watch the per-field
   log, and add a fixture for anything that breaks rather than only tuning live.

3. **Deploy** — I have no Fly auth and wouldn't deploy unasked. Note the schema
   gained `user_knowledge`; the migration is idempotent and runs at import.

4. **Tune thresholds to your taste.** `fit.py` explains *why* a job surfaced, which
   should make it obvious when the threshold is wrong — but only you can say
   whether a given recommendation was actually good. That's judgment, not a test.

5. **Teach it about you.** The knowledge store is empty. Until you add a few
   projects and achievements, drafted answers stay as generic as they were —
   the machinery is in place, the content is yours. Start with
   `what do you know about me` to see what's missing.

---

## Notes

- `playwright` installed into the shared venv; already in `worker/requirements.txt`,
  not added to the main one. Browser tests `importorskip` so a browserless CI passes.
- `.env.example` untouched.
- `verify.sh` fixed — it used `timeout`, which macOS doesn't ship.
- I did not push, PR, deploy, or touch prod, Fly, a live ATS, or a real Slack
  workspace.
