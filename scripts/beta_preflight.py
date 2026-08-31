#!/usr/bin/env python3
"""Check a running deployment against the invite-beta checklist.

`handoff.md` §5 says of the Fly secrets: "Verify it, don't assume it." That is
the whole reason this exists. Every paid call site in this app fails open to a
heuristic or a template, which is the right behaviour at 3am and the wrong
behaviour when you are deciding whether to hand a build to someone: a revoked
key, a typo'd model id, or a missing base résumé degrades the product silently
and `/health` still says 200.

So this reads the checklist off the live service and prints a verdict per item.
It never writes anything and never sends anyone an invite.

Usage:
    .venv/bin/python -m scripts.beta_preflight                     # localhost
    .venv/bin/python -m scripts.beta_preflight --url https://job-search-tool.fly.dev
    .venv/bin/python -m scripts.beta_preflight --url ... --token "$APPLY_API_TOKEN"
    .venv/bin/python -m scripts.beta_preflight --url ... --token ... --spend

`--spend` adds the one check that costs real money: GET /health/llm makes a
single tiny live call, which is the only way to tell a revoked key from a good
one. Everything else is free.

Exit code is 0 when nothing BLOCKING failed, 1 otherwise — so this can gate a
release step.
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

# Checks are either BLOCKING (do not invite anyone) or ADVISORY (a feature is
# degraded, the beta still works). The split matters: the point of the script
# is to stop guesswork, not to make every yellow light look like a red one.
BLOCKING, ADVISORY = "BLOCK", "WARN"

OK = "  ok  "
FAIL = " FAIL "
WARN = " warn "
SKIP = " skip "


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []
        self.blocking_failures = 0

    def add(self, mark: str, name: str, detail: str = "") -> None:
        self.rows.append((mark, name, detail))
        if mark == FAIL:
            self.blocking_failures += 1

    def check(self, ok: bool, severity: str, name: str,
              ok_detail: str = "", bad_detail: str = "") -> bool:
        if ok:
            self.add(OK, name, ok_detail)
        else:
            self.add(FAIL if severity == BLOCKING else WARN, name, bad_detail)
        return ok

    def render(self) -> None:
        width = max(len(n) for _, n, _ in self.rows) + 2
        for mark, name, detail in self.rows:
            print(f"[{mark}] {name:<{width}} {detail}")


def _get(client: httpx.Client, path: str, token: str | None) -> tuple[int, dict]:
    headers = {"X-Apply-Token": token} if token else {}
    try:
        r = client.get(path, headers=headers, timeout=30)
    except httpx.HTTPError as exc:
        return 0, {"error": str(exc)}
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"error": r.text[:200]}


def evaluate(health: dict) -> Report:
    """Grade a /health payload. Split out from the fetch so the verdict logic —
    which items block an invite and which only degrade a feature — is testable
    without a running server."""
    rep = Report()
    auth = health.get("auth", {})
    beta = health.get("beta", {})
    deps = health.get("dependencies", {})
    llm = health.get("llm", {})

    # --- Isolation: the checks that decide whether data can leak ---------
    rep.check(auth.get("fail_open") is False, BLOCKING,
              "AUTH_FAIL_OPEN is off",
              bad_detail="?user= is honoured without a session — set AUTH_FAIL_OPEN=false")
    rep.check(auth.get("dev_login") is False, BLOCKING,
              "Dev login is off",
              bad_detail="POST /auth/dev mints sessions — set AUTH_ALLOW_DEV_LOGIN=false")
    rep.check(bool(auth.get("allowlist")), BLOCKING,
              "Invite allowlist is set",
              bad_detail="AUTH_ALLOWED_EMAILS is empty — anyone with an Apple ID can sign in")
    rep.check(bool(beta.get("invite_ready")), BLOCKING,
              "beta.invite_ready",
              bad_detail="do not invite anyone until this is true")

    # Both doors are gated by the same allowlist, so an empty allowlist with
    # email sign-up on is the wider hole of the two.
    methods = auth.get("methods") or []
    rep.add(OK, "Sign-in methods", ", ".join(methods) or "apple")
    if auth.get("email_signup") and not auth.get("allowlist"):
        rep.add(FAIL, "Email sign-up is open",
                "AUTH_ALLOW_EMAIL_SIGNUP is on with no allowlist — anyone can create an account")

    rep.check(bool(health.get("db_ok")), BLOCKING, "Database writable",
              ok_detail=str(health.get("db", "")),
              bad_detail="the volume is not writable — reminders and sign-ups will fail")
    rep.check(health.get("reminder_delivery") == "app", BLOCKING,
              "Reminders deliver in-app",
              bad_detail=f"reminder_delivery={health.get('reminder_delivery')!r}")

    # --- Quality: the silent-degradation checks --------------------------
    problem = llm.get("problem")
    rep.check(problem is None, ADVISORY, "Anthropic key configured",
              ok_detail=f"model {llm.get('model') or health.get('model') or '?'}",
              bad_detail=str(problem))
    rep.check(bool(beta.get("llm_ready")), ADVISORY, "beta.llm_ready",
              bad_detail="every AI feature is running on heuristics")

    missing = deps.get("missing") or []
    rep.check(not missing, ADVISORY, "Python dependencies present",
              bad_detail=f"missing: {', '.join(missing)}")

    rep.check(bool(auth.get("sentry")), ADVISORY, "Sentry DSN set",
              bad_detail="crashes from testers will go nowhere")

    push = health.get("push")
    if isinstance(push, dict):
        missing_apns = push.get("missing") or []
        rep.check(bool(push.get("active")), ADVISORY, "Push (APNs) active",
                  bad_detail=(f"missing {', '.join(missing_apns)}" if missing_apns
                              else "PUSH_ENABLED is off — no new-match pings"))
        # TestFlight builds ship the production APNs entitlement, so a sandbox
        # host means every push is silently rejected.
        if push.get("active") and push.get("sandbox"):
            rep.check(False, ADVISORY, "APNs host",
                      bad_detail="APNS_USE_SANDBOX=true — set false for TestFlight")

    resume = health.get("resume")
    if isinstance(resume, dict) and resume.get("enabled"):
        # `or` would swallow the empty list, which is the case that matters.
        bases = resume.get("bases")
        expected = resume.get("expected") or []
        missing_tex = [b for b in expected if b not in (bases or [])]
        rep.check(not missing_tex, ADVISORY, "Base résumés on the volume",
                  ok_detail=", ".join(bases or []),
                  bad_detail=(f"missing {', '.join(missing_tex)} in "
                              f"{resume.get('dir', '/data/resumes')}"))

    return rep


def run(url: str, token: str | None, spend: bool) -> int:
    with httpx.Client(base_url=url.rstrip("/"), follow_redirects=True) as client:
        status, health = _get(client, "/health", token)
        if status != 200:
            print(f"[{FAIL}] /health unreachable at {url}: {health.get('error', status)}")
            return 1

        rep = evaluate(health)

        # --- The one check that costs money ----------------------------------
        if spend:
            status, body = _get(client, "/health/llm", token)
            if status == 401:
                rep.add(WARN, "Live LLM call",
                        "401 — pass --token \"$APPLY_API_TOKEN\" to run this check")
            elif status == 200 and body.get("ok"):
                rep.add(OK, "Live LLM call", f"answered with {body.get('model', '?')}")
            else:
                rep.add(WARN, "Live LLM call",
                        f"{status}: {body.get('problem') or body.get('error') or body}")
        else:
            rep.add(SKIP, "Live LLM call", "pass --spend to make one real call")

        rep.add(OK, "Reported status", str(health.get("status")))

    print(f"\nBeta preflight — {url}\n")
    rep.render()

    print()
    if rep.blocking_failures:
        print(f"{rep.blocking_failures} blocking problem(s). Do not invite testers yet.")
        return 1
    warns = sum(1 for m, _, _ in rep.rows if m == WARN)
    if warns:
        print(f"No blockers. {warns} degraded item(s) above — the beta works, "
              f"but those features are running on fallbacks.")
    else:
        print("All checks passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8000",
                    help="base URL of the deployment (default: local uvicorn)")
    ap.add_argument("--token", default=None,
                    help="APPLY_API_TOKEN, needed for /health/llm")
    ap.add_argument("--spend", action="store_true",
                    help="make one real (paid) Anthropic call to prove the key works")
    ap.add_argument("--json", action="store_true",
                    help="dump the raw /health payload instead of a verdict")
    args = ap.parse_args()

    if args.json:
        with httpx.Client(base_url=args.url.rstrip("/"), follow_redirects=True) as c:
            _, body = _get(c, "/health", args.token)
        print(json.dumps(body, indent=2))
        return 0
    return run(args.url, args.token, args.spend)


if __name__ == "__main__":
    sys.exit(main())
