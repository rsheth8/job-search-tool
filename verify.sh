#!/usr/bin/env bash
# 30-second morning check: did the overnight build land clean and safe?
# Usage:  ./verify.sh            (full)   |   ./verify.sh --fast  (skip browser tests)
set -uo pipefail
cd "$(dirname "$0")"

VP=.venv/bin/python
[ -x "$VP" ] || VP=/Users/rahilsheth/Documents/job-search-tool/.venv/bin/python
BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'; YEL=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
ok(){ echo "${GREEN}✔${OFF} $*"; }
bad(){ echo "${RED}✗${OFF} $*"; }
hdr(){ echo; echo "${BOLD}$*${OFF}"; }

hdr "① Where the build left off"
echo "  branch: ${BOLD}$(git rev-parse --abbrev-ref HEAD)${OFF}"
git --no-pager log --oneline -8 | sed 's/^/  /'
echo "${DIM}  working tree:${OFF}"
git status --short | sed 's/^/  /' || true

# Browser-driven tests (iOS autofill, the unbiased corpus, Python/JS rule
# parity) launch Chromium. These are the only tests that run the *shipping*
# autofill JavaScript, so a silent skip here is how a real fill bug ships.
BROWSER_TESTS="tests/test_ios_autofill.py tests/test_autofill_corpus.py tests/test_rules_parity.py"
SKIP=""
[ "${1:-}" = "--fast" ] && for f in $BROWSER_TESTS; do SKIP="$SKIP --ignore=$f"; done

hdr "② Test suite"
if "$VP" -m pytest -q $SKIP 2>&1 | tail -12 | sed 's/^/  /'; then
  ok "pytest exited clean"
  [ -n "$SKIP" ] && echo "${DIM}  (browser tests skipped by --fast)${OFF}"
else
  bad "pytest FAILED — read the tail above before trusting anything else"
fi

hdr "③ Browser-driven safety tests (iOS autofill · corpus · rule parity)"
if [ "${1:-}" = "--fast" ]; then
  echo "${YEL}  skipped (--fast)${OFF}"
else
  OUT=$("$VP" -m pytest $BROWSER_TESTS -q 2>&1)
  echo "$OUT" | tail -6 | sed 's/^/  /'
  # Without Chromium these tests skip and pytest still exits 0 — green, with the
  # autofill engine never once executed. Say so instead of looking clean.
  if echo "$OUT" | grep -qE "^[0-9]+ skipped|no tests ran" \
     && ! echo "$OUT" | grep -qE "[0-9]+ passed"; then
    bad "the autofill engine never ran — these tests all skipped"
    echo "${YEL}  fix:${OFF} $VP -m playwright install chromium"
  else
    ok "the shipping autofill JavaScript ran against real forms"
  fi
fi

hdr "④ Safety invariants (never fill EEO; human submits)"
grep -rl -i -E "eeo|demographic|never.?submit|not.*submit" tests/ >/dev/null 2>&1 \
  && grep -ric -E "eeo|demographic" tests/test_ios_autofill.py tests/test_fieldmatch.py 2>/dev/null \
     | sed 's/^/  /' \
  || bad "no safety-invariant assertions found — check this by hand"

hdr "⑤ Boot the app (smoke test the server starts)"
if "$VP" -c "from app.main import app; print('routes:', len(app.routes))" 2>&1 | sed 's/^/  /'; then
  ok "app imports cleanly"
else
  bad "app failed to import"
fi

echo; echo "${BOLD}Done.${OFF} To run the app live:  ${DIM}$VP -m uvicorn app.main:app --reload${OFF}"
