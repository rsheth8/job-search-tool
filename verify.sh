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

hdr "② Full test suite (must stay green — baseline ~496 passed)"
if "$VP" -m pytest -q 2>&1 | tail -12 | sed 's/^/  /'; then
  ok "pytest exited clean"
else
  bad "pytest FAILED — read the tail above before trusting anything else"
fi

hdr "③ Worker fill/safety tests (the overnight target)"
if ls tests/test_worker_fill.py >/dev/null 2>&1; then
  if [ "${1:-}" = "--fast" ]; then
    echo "${YEL}  skipped (--fast)${OFF}"
  else
    "$VP" -m pytest tests/test_worker_fill.py tests/test_fieldmatch.py tests/test_agent.py -q 2>&1 \
      | tail -8 | sed 's/^/  /'
  fi
else
  bad "tests/test_worker_fill.py not found — the worker-hardening track didn't land"
fi

hdr "④ Safety invariants still asserted (never auto-submit, never fill EEO)"
grep -rl -i -E "eeo|demographic|never.?submit|not.*submit|approve" tests/ >/dev/null 2>&1 \
  && grep -ric -E "eeo|demographic|submit" tests/test_worker_fill.py tests/test_agent.py 2>/dev/null \
     | sed 's/^/  /' \
  || bad "no safety-invariant assertions found — check this by hand"

hdr "⑤ What the build wrote up for you"
for f in WORKER_HARDENING_NOTES.md OVERNIGHT_NOTES.md; do
  [ -f "$f" ] && { ok "$f"; echo "${DIM}     open it for the full before/after + what still needs you${OFF}"; }
done

hdr "⑥ Boot the app (smoke test the server starts)"
timeout 12 "$VP" -c "from app.main import app; print('  app imports OK — routes:', len(app.routes))" 2>&1 \
  | sed 's/^/  /' || bad "app failed to import"

echo; echo "${BOLD}Done.${OFF} To run the app live:  ${DIM}$VP -m uvicorn app.main:app --reload${OFF}"
