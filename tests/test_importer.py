"""Bulk backfill import (offline — heuristic router, temp DB)."""
from __future__ import annotations

from datetime import datetime, timezone

from app import importer, scoring, store


# --- brain-dump -------------------------------------------------------------

def test_braindump_creates_applications():
    text = """
    applied stripe swe
    applied to notion pm
    - applied figma designer
    """
    s = importer.import_braindump("u", text)
    assert s.created == 3
    companies = {a["company"] for a in store.list_applications("u")}
    assert {"Stripe", "Notion", "Figma"} <= companies


def test_braindump_update_after_apply():
    importer.import_braindump("u", "applied stripe swe")
    s = importer.import_braindump("u", "stripe oa received")
    assert s.updated == 1
    app = store.find_application("u", "Stripe")
    assert app["status"] == "OA received"


def test_braindump_dedupes_repeat_lines():
    importer.import_braindump("u", "applied stripe swe")
    s = importer.import_braindump("u", "applied stripe swe")
    assert s.created == 0
    assert s.skipped and "already tracked" in s.skipped[0][1]


def test_braindump_skips_uninterpretable_line():
    s = importer.import_braindump("u", "the weather is nice today")
    assert s.created == 0
    assert len(s.skipped) == 1


def test_braindump_note_attaches_when_company_known():
    importer.import_braindump("u", "applied ramp swe")
    s = importer.import_braindump("u", "note ramp referred by a friend")
    assert s.noted == 1


# --- CSV --------------------------------------------------------------------

def test_csv_import_with_headers_and_dates():
    csv_text = (
        "company,role,status,applied_at,notes\n"
        "Stripe,SWE,OA received,2026-05-01,strong rec\n"
        "Notion,PM,Applied,05/10/2026,\n"
        ",,,,orphan row\n"
    )
    s = importer.import_csv("u", csv_text)
    assert s.created == 2
    assert s.noted == 1  # the "strong rec" note
    assert len(s.skipped) == 1  # blank-company row

    stripe = store.find_application("u", "Stripe")
    assert stripe["status"] == "OA received"
    # applied_at parsed and stored (drives staleness, not "now").
    assert stripe["applied_at"].startswith("2026-05-01")


def test_csv_status_normalized():
    s = importer.import_csv("u", "company,status\nGoogle,final round\n")
    assert s.created == 1
    assert store.find_application("u", "Google")["status"] == "Onsite"


def test_csv_case_insensitive_headers():
    s = importer.import_csv("u", "Company,Role\nVercel,Backend\n")
    assert s.created == 1
    assert store.find_application("u", "Vercel")["role"] == "Backend"


# --- integration with scoring (backfill powers follow-ups) ------------------

def test_imported_dates_feed_staleness_scoring():
    old = "2026-01-01"
    importer.import_csv("u", f"company,status,applied_at\nOldCo,Applied,{old}\n")
    importer.import_braindump("u", "applied freshco swe")  # today
    now = datetime(2026, 5, 30, tzinfo=timezone.utc)
    ranked = scoring.rank_followups("u", now=now)
    top_company = ranked[0][0]["company"]
    assert top_company == "OldCo"  # stale-since-January outranks today's app


# --- summary rendering ------------------------------------------------------

def test_summary_render_lists_skips():
    s = importer.ImportSummary(created=2, updated=1, skipped=[("junk line", "couldn't interpret")])
    out = s.render()
    assert "2 added" in out
    assert "1 updated" in out
    assert "junk line" in out
