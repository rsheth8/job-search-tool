"""Regenerate the offline rule tables bundled in ``ios/JobPilot/Autofill.swift``.

The autofill matcher lives in ``app/fieldmatch.py``; the phone fetches it from
``/apply/rules`` and keeps a bundled copy for a first launch or a dropped
connection. That copy is a hand-written JS literal, so historically it drifted
-- and because the served rules normally win, drift stayed invisible until
someone was offline. ``tests/test_rules_parity.py`` catches it now, and this
script is how you fix it:

    python -m scripts.gen_autofill_fallback

It rewrites only the three ``FALLBACK_*`` blocks and leaves the rest of the
file alone. ``--check`` exits non-zero if the file would change.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

from app import fieldmatch

TARGET = pathlib.Path(__file__).resolve().parents[1] / "ios/JobPilot/Autofill.swift"
INDENT = " " * 8


def _rule_lines(rules) -> str:
    # Every pattern is already written in the Python/JS common subset (see
    # rules_payload), so the only translation needed is the regex literal's
    # own delimiter.
    out = []
    for key, pattern in rules:
        out.append(f'{INDENT}["{key}", /{pattern.replace("/", chr(92) + "/")}/i],')
    return "\n".join(out)


def render(source: str) -> str:
    payload = fieldmatch.rules_payload()
    blocks = {
        "FALLBACK_RULES": _rule_lines(payload["rules"]),
        "FALLBACK_ATTR_RULES": _rule_lines(payload["attr_rules"]),
    }
    for name, body in blocks.items():
        pattern = re.compile(
            r"(const " + name + r" = \[\n).*?(\n\s*\];)", re.S)
        assert pattern.search(source), f"no {name} block in {TARGET.name}"
        source = pattern.sub(lambda m: m.group(1) + body + m.group(2), source, count=1)

    # Lambda replacements throughout: these patterns are full of backslashes
    # (\bdob\b), and a plain replacement string would read them as escapes --
    # \b became a literal backspace and the parity test caught it.
    eeo = payload["never_fill"].replace("/", chr(92) + "/")
    source = re.sub(r"const FALLBACK_EEO = /.*?/i;",
                    lambda _m: f"const FALLBACK_EEO = /{eeo}/i;",
                    source, count=1, flags=re.S)
    # The header comment quotes the version so a reviewer can see at a glance
    # whether the bundle is current.
    source = re.sub(r"(generated from app/fieldmatch\.py \(rules version\n\s*//\s*)[0-9a-f]+",
                    lambda m: m.group(1) + payload["version"], source, count=1)
    return source


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if the bundled rules are stale")
    args = ap.parse_args(argv)

    before = TARGET.read_text()
    after = render(before)
    if args.check:
        if before != after:
            print(f"{TARGET} is stale — run python -m scripts.gen_autofill_fallback")
            return 1
        print("bundled autofill rules are current")
        return 0
    if before == after:
        print("no change")
        return 0
    TARGET.write_text(after)
    print(f"rewrote {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
