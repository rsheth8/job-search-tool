"""Backfill full job descriptions for already-labeled postings.

Older swipe labels stored only the ~280-char card snippet. Embeddings need the
full text, so this re-fetches each ATS board (greenhouse/lever/ashby) and updates
``training_labels.description`` with the full posting body where we can still find
it. RSS-sourced labels can't be re-fetched per-item and are skipped.

Read-only against the network (free ATS APIs); only writes longer descriptions.
Run:  python -m scripts.backfill_descriptions
"""
from __future__ import annotations

from app.db import connect
from app.jobsources import fetch_source

_ATS = ("greenhouse", "lever", "ashby")


def _parse(external_id: str) -> tuple[str | None, str | None]:
    """'greenhouse:airbnb:12345' -> ('airbnb', '12345'). The deck namespaces ids
    as '{source}:{board}:{rawid}'."""
    parts = (external_id or "").split(":")
    if len(parts) >= 3:
        return parts[1], ":".join(parts[2:])
    return None, None


def main() -> None:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, source, external_id, description FROM training_labels "
            f"WHERE source IN ({','.join('?' * len(_ATS))})",
            _ATS,
        ).fetchall()

    # Group label rows by the board we'd need to re-fetch.
    by_board: dict[tuple[str, str], list[tuple[int, str]]] = {}
    skipped = 0
    for r in rows:
        board, rawid = _parse(r["external_id"])
        if not board or not rawid:
            skipped += 1
            continue
        by_board.setdefault((r["source"], board), []).append((r["id"], rawid))

    print(f"{len(rows)} ATS labels across {len(by_board)} boards "
          f"(skipped {skipped} un-parseable)")

    updated = gone = 0
    for (source, board), items in sorted(by_board.items()):
        live = {p.external_id: (p.description or "") for p in fetch_source(source, board)}
        with connect() as conn:
            for label_id, rawid in items:
                full = live.get(rawid)
                if full and len(full) > 300:
                    conn.execute(
                        "UPDATE training_labels SET description = ? WHERE id = ?",
                        (full, label_id),
                    )
                    updated += 1
                else:
                    gone += 1  # posting closed / not found / already short
        print(f"  {source}/{board}: {len(items)} label(s)")

    print(f"\nUpdated {updated} description(s); {gone} unavailable (closed/removed).")


if __name__ == "__main__":
    main()
