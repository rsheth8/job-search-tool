#!/usr/bin/env python3
"""Local REPL to talk to the system exactly like SMS would — no Twilio needed.

    python cli.py                # interactive
    python cli.py "applied spotify swe ii"   # one-shot
    python cli.py import apps.csv            # bulk import (.csv → CSV, else brain-dump)
    python cli.py import -                    # brain-dump from stdin (paste, then Ctrl-D)

Uses user_id="local" so it shares context/data with the in-app chat flow.
"""
from __future__ import annotations

import sys

from app.db import init_db
from app.engine import handle_sms

USER = "local"


def _send(text: str) -> None:
    print(f"\n> {text}")
    print(handle_sms(USER, text))


def _do_import(args: list[str]) -> None:
    from app import importer

    if not args:
        print("Usage: python cli.py import <file.csv|file.txt|->")
        return
    source = args[0]
    if source == "-":
        text, as_csv = sys.stdin.read(), False
    else:
        with open(source, encoding="utf-8") as fh:
            text = fh.read()
        as_csv = source.lower().endswith(".csv")
    summary = importer.import_csv(USER, text) if as_csv else \
        importer.import_braindump(USER, text)
    print(summary.render())


def main() -> None:
    init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "import":
        _do_import(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "agenda":
        from app import deadlines
        print(deadlines.render_upcoming(USER))
        return
    if len(sys.argv) > 1:
        _send(" ".join(sys.argv[1:]))
        return
    print("Job-search SMS REPL. Type a message (Ctrl-C / 'quit' to exit).")
    try:
        while True:
            text = input("\nsms> ").strip()
            if text.lower() in {"quit", "exit"}:
                break
            if text:
                print(handle_sms(USER, text))
    except (EOFError, KeyboardInterrupt):
        print("\nbye")


if __name__ == "__main__":
    main()
