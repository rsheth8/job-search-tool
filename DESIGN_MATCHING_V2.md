# Design — Matching v2 (embeddings → personalized re-ranker → ghost-job filter)

> Build target: `origin/main` (has resume-tailoring + `posting_match.py`), NOT the
> stale `integrate-discovery` worktree. Sync first.
> Constraints carried from `handoff.md`: Anthropic-only for LLM, Haiku 4.5, runs on
> a 512MB Fly box, free heuristic path must keep working (it's the CI path), every
> paid call gated + budget-capped.

## Why not train an embedding model from scratch

Embedding models need millions of pairs; we have dozens–hundreds of labeled
applications. A from-scratch (or even fine-tuned) embedding model would
underperform off-the-shelf and add GPU/training/versioning infra to a 512MB box.
The right-sized "own model" is a **small re-ranker on top of pretrained
embeddings**, trained on the apply/dismiss/snooze labels we already collect.

## Where this slots into the current pipeline

Today (`app/discovery.py` `tick`):
```
fetch → dedupe → quality.filter_reputable → matcher.prefilter → cap → matcher.score → persist → alert
```
v2:
```
fetch → dedupe → ghost_filter (3) → quality.filter_reputable → matcher.prefilter
      → cap → embed + score (1) → rerank (2) → persist → alert
```
`matcher.score` stays the contract `[(posting, score)] ` — we change what fills the
score, not the surface. Keyword `prefilter` stays as the free cost gate.

---

## Phase 1 — Embedding-based semantic scoring — DONE (2026-06-08)

Shipped on branch `matching-v2`:
- `app/embeddings.py` — Voyage backend, pure-Python cosine + float32 BLOB
  (de)serialization, never-raises `embed()` (gated by `embedding_active`, daily
  cap via `jobstore.allow_embedding_call`, per-min TokenBucket).
- `app/config.py` — `embedding_enabled` / `voyage_api_key` / `embedding_model` /
  `embedding_max_calls_per_day` / `embedding_rate_limit_per_min` + `embedding_active`.
- `app/db.py` — `job_postings.embedding BLOB` + idempotent migration.
- `app/jobsources/base.py` — `JobPosting.embedding` field (excluded from dedupe).
- `app/matcher.py` — `_embedding_score` woven into `score()` ahead of LLM/heuristic
  (injectable `embedder=` for tests); stashes the vector for persistence.
- `app/jobstore.py` — persists the vector in `save_posting`; `allow/record/_today`
  embedding-call helpers.
- `app/main.py` — `/health` `embeddings` block when active.
- `tests/test_embeddings.py` (11) — math, blob roundtrip, gating/degrade, matcher
  integration. Full suite **357 passing**.

**Follow-up (not yet done):** cosine→threshold calibration. Cosine magnitudes
don't line up with the keyword-heuristic scale, so `job_relevance_threshold` /
per-user TUNE likely needs re-tuning once embeddings run against live postings.
Optional Haiku top-N re-rank also deferred.

### Original design notes

**Goal:** replace the keyword-ratio heuristic with cosine similarity between the
candidate profile (profile_text + base resume) and each JD. Keep Haiku as an
optional final re-rank on the top N.

**Embedding backend — decision needed (see questions):**
- **Option A — Voyage API** (`voyage-3-lite`, Anthropic's recommended embeddings).
  No local model weight, tiny deps, ~$0.02/1M tokens. Network dependency + cost,
  gated/capped like the aggregator.
- **Option B — local `sentence-transformers` (`all-MiniLM-L6-v2`, ~80MB)**. Free,
  offline, but pulls in torch (~heavy for 512MB; may need the box bumped or
  `onnxruntime` quantized export). Best if we want $0 marginal cost.

Recommendation: **A** for simplicity now (we already gate paid calls); revisit B if
cost matters. Either way, hide it behind an `Embedder` protocol so it's swappable
and the test path uses a deterministic fake (hash-based vectors).

**New module `app/embeddings.py`:**
- `embed(texts: list[str]) -> list[list[float]]` — batched, cached, never raises
  (returns `[]`/`None` on failure so scoring degrades to the keyword heuristic).
- `cosine(a, b)`.
- DB-backed cache: store JD vectors so we embed each posting once (mirrors the
  "score/alert once" dedupe rule).

**Storage:** new `job_postings.embedding` BLOB column (numpy `tobytes`), migration
as an idempotent ALTER (same pattern as `snoozed_until`/`min_relevance`).
Brute-force cosine in numpy is fine at our scale — no ANN/`sqlite-vec` needed until
thousands of live postings.

**Profile vector:** embed `profile_text(profile)` + the relevant base resume body
(reuse `resume_tailor` variant pick: swe vs aiml). Cache per-profile, invalidate on
profile edit.

**`matcher.score` change:** when embeddings available, `base = cosine(profile_vec,
jd_vec)` (rescaled to a sane 0..1), blended with the existing location/keyword
signal. Haiku re-rank optional on top-K (config flag) for the final ordering.

**Tests:** fake embedder (deterministic), cosine math, cache hit (embed-once),
graceful fallback when backend returns nothing, migration idempotency.

---

## Deck TL;DR insights — DONE (2026-06-08)

Plain-language "what is this role + is it for me" on each swipe card, credit-
efficient by design.
- `app/insights.py` — `enrich(cards, profile_block)`: cache-first, ONE batched
  Haiku call for misses, fail-open. `summarize_batch` (schema: per-id tldr ≤22w +
  fit ≤12w); injectable `summarize=` in tests.
- `app/db.py` — `posting_summaries` cache table (summarize once ever).
- `app/jobstore.py` — `allow/record_summary_call` (daily cap in decks/day).
- `app/main.py` — `/train/deck` enriches cards; `/health` `deck_tldr` flag.
- `app/trainer.py` — card UI shows TL;DR + FIT blocks.
- `app/config.py` — `deck_tldr_enabled` (off) / `deck_tldr_max_calls_per_day`.
- `tests/test_insights.py` (5). Full suite **405 passing**.
Efficiency: batched (1 call/deck), cached by posting, Haiku, ~320-char inputs,
gated + capped. Enable: `DECK_TLDR_ENABLED=true` (reuses ANTHROPIC_API_KEY).

## Eligibility / qualification gate — DONE (2026-06-08)

Answers "could this candidate realistically *do / be considered for* this role?",
distinct from the matcher's "is it a good fit?". Profile-driven (candidate level
from profile seniority, fallback `eligibility_candidate_level`).
- `app/eligibility.py` — rule tier (free, on): seniority-gap (role rank −
  candidate rank ≥ 2), big years-of-experience asks (scales with level), hard
  credentials (nursing/CPA/clearance/required doctorate). LLM tier (off; batched
  Haiku, gated + daily-capped, **fail-open**) for nuance; injectable judge in tests.
- `app/discovery.py` — rule tier after ghost gate (before prefilter); LLM tier on
  the capped candidate set. `app/jobstore.py` — eligibility daily-cap helpers.
- `app/config.py` — `eligibility_filter_enabled` (on) / `_candidate_level` /
  `_llm_enabled` (off) / `_max_calls_per_day`. `app/main.py` — `/health` flags.
- `tests/test_eligibility.py` (13). Full suite **400 passing**.

Validated against real swipe data: correctly drops Analytics Lead/Manager &
Compliance Lead, keeps Analyst II / Associate. Customized — the same Senior role
is dropped for an entry candidate, kept for a senior one.

## Swipe trainer — bootstrap the re-ranker — DONE (2026-06-08)

Cold-start tool so the re-ranker has labels before the user has applied to much.
A Tinder-style web UI at `/train` shows **real** postings (from the ATS directory)
and records a quick "would I apply?" yes/no per card.
- `app/trainer.py` — `build_deck` (directory postings, scored, deduped vs already-
  swiped), `record_label`, `stats`, `render_page` (self-contained HTML/JS, no deps).
- `app/db.py` — `training_labels` table (separate from `job_postings` so swipes
  never look like real applications).
- `app/reranker.py` — `_labeled_examples` now reads training_labels alongside
  real applications (swipe-'like'→1, swipe-'pass'→0); `_label_count` counts both.
- `app/main.py` — `GET /train` (UI), `GET /train/deck`, `POST /train/label`
  (records + retrains + returns stats).
- `tests/test_trainer.py` (9). Full suite **387 passing**.

Usage: run the server, open `/train?user=<your id>`, swipe ~10+ (≥5 yes, ≥5 no);
the model trains automatically. Set `RERANKER_ENABLED=true` for discovery to use it.

## Phase 2 — Personalized re-ranker — DONE (2026-06-08)

Shipped on branch `matching-v2`:
- `app/reranker.py` — pure-Python L2 logistic regression (NOT scikit-learn, to
  keep the box light); `Featurizer` reuses the matcher's term/location logic for
  6 free features (relevance, kw_overlap, title_hit, loc_match, is_remote,
  first_party); `train` / `maybe_retrain` / `rerank` / model persistence.
  Labels: applied=1, dismissed=0, snoozed=0 @ half weight. Cold-start safe (no-op
  below per-class minimums); never raises.
- `app/db.py` — `reranker_models` table (one JSON model per user).
- `app/config.py` — `reranker_enabled` (default off) + `reranker_min_positive` /
  `reranker_min_negative`. `app/discovery.py` — `maybe_retrain` + `rerank` after
  scoring (gated). `app/main.py` — `/health` flag.
- `tests/test_reranker.py` (10). Full suite **378 passing**.

Decision: chose **pure-Python LR** over sklearn (avoids numpy+scipy ~100MB on the
512MB box; same math at this data scale). Retrains in-place per tick when the
label count changes — cheap. **Deferred:** a hold-out "don't promote a worse
model" guard (noisy at small N; L2 regularization covers overfit for now).

### Original design notes

**Goal:** learn *your* preferences from the labels you already produce, so ranking
reflects what you actually apply to vs dismiss — not just generic similarity.

**Labels (from `job_postings.status`):**
- positive: `applied`
- negative: `dismissed`
- weak signals: `snoozed` (mild negative / "not now"), `alerted`-but-ignored
  (weak negative after N days), `tune` threshold changes (global, not per-item).

**Features per (profile, posting):**
- embedding cosine (Phase 1)
- keyword overlap ratio (existing `_heuristic_score` numerator)
- location match / remote flag
- seniority match
- source reputation tier (`quality.FIRST_PARTY_SOURCES` etc.)
- posting age, description length
- (optional) raw embedding delta vector for a small MLP

**Model:** `scikit-learn` `LogisticRegression` (or tiny MLP) — trains in <1s on CPU,
interpretable coefficients, retrains as labels grow. Persist pickle on the Fly
volume (like base resumes). New module `app/reranker.py`:
- `train(user_id)` — pulls labeled rows, fits, saves; no-op below a min-label
  threshold (cold start).
- `rank(postings, profile)` — returns calibrated 0..1; **falls back to Phase-1
  similarity below the label threshold** (cold start) so new users still get good
  results.
- retrain trigger: on every Nth new label, or a daily scheduler job.

**Cold-start:** until ~50 labels, use Phase-1 similarity directly. The re-ranker
only kicks in once it can beat that (hold-out check before promoting a new model).

**Tests:** synthetic labeled set → learns the obvious signal; cold-start fallback;
model persist/load; "don't promote a worse model" guard.

---

## Phase 3 — Ghost-job / fake-listing filter — DONE (2026-06-08)

Shipped on branch `matching-v2`:
- `app/jobsources/ghost.py` — weighted, conservative content rules
  (`ghost_signals` / `ghost_score` / `is_ghost`, threshold 0.6): evergreen/
  pipeline language, personal-email contact, comp hype, staleness (ISO + "30+
  days ago"), thin description, plus a caller-supplied repost signal. First-party
  ATS always trusted (`quality.is_first_party`).
- `app/jobstore.py` — `seen_similar_count` (repost count via `posting_match`
  company+title similarity).
- `app/discovery.py` — ghost gate after the reputability gate, before scoring.
- `app/config.py` — `ghost_filter_enabled` (default on). `app/main.py` — `/health`.
- `tests/test_ghost.py` (11). Full suite **368 passing**.

Each signal is sub-threshold on its own except the high-precision ones (evergreen,
personal email, 3+ reposts) — so a single real posting is never dropped without
strong evidence. LLM-on-borderline still deferred.

### Original design notes

**Goal:** beyond current spam rules in `quality.py`, catch ghost jobs (reposted,
never-filled, staffing churn) and scams.

**Signals:**
- **Repost / staleness:** same normalized (company, title) seen repeatedly or
  alive for many weeks → ghost. Reuse `posting_match.py` normalization; track
  first-seen and repost count (new `job_postings.first_seen_at` / `repost_count`,
  or a small `posting_fingerprints` table).
- **Description red flags:** vague/boilerplate, "always hiring", contact via
  personal email, no real company domain in the apply URL, unrealistic comp.
- **Company verifiability:** apply URL domain vs company name mismatch; known
  staffing-agency domains.

**Implementation:** start as **weighted rules** extending `quality.py`
(`ghost_score(p) -> 0..1`, drop above a conservative threshold — same "never drop a
real employer" stance as today). Optional Haiku classifier on borderline cases
only (gated/capped), reusing the batched-call pattern from `matcher`.

**Tests:** ghost/repost detection on fixtures, real first-party posting survives,
threshold conservatism, LLM-borderline path mocked.

---

## Cross-cutting

- **Config:** `embedding_backend`, `embedding_enabled`, `rerank_enabled`,
  `ghost_filter_enabled`, plus daily caps for any paid embedding/LLM calls.
- **/health:** add embedding cache size, re-ranker label count + last-trained,
  ghost-drop counts (mirrors existing aggregator block).
- **Free path intact:** with everything disabled/keyless, behavior == today's
  heuristic. CI never hits a network or a real model.
- **Migrations:** all idempotent ALTERs; prod SQLite on the Fly volume picks them
  up on next deploy.

## Suggested build order

1. Phase 1 (embeddings) — unlocks both ranking quality and Phase-2 features.
2. Phase 3 (ghost filter) — independent, quick, immediately improves alert quality.
3. Phase 2 (re-ranker) — last; needs Phase-1 features + accumulated labels.

## Decisions (locked 2026-06-08)

1. **Embedding backend: Voyage API** (`voyage-3-lite`). Behind an `Embedder`
   protocol; deterministic fake in tests. Gated + daily-capped like the aggregator
   (`embedding_api_key`, `embedding_enabled`, `embedding_max_calls_per_day`).
2. **Re-ranker: scikit-learn `LogisticRegression`.** Interpretable coefficients,
   instant retrain, cold-start fallback to Phase-1 similarity below the label
   threshold.
3. **Ghost filter: rules-first.** Weighted heuristics extending `quality.py`, no
   API. Haiku-on-borderline deferred until rules prove insufficient.
