"""Merging a user's data from one id into another (account consolidation)."""
from __future__ import annotations

from app import applicant, jobstore, profile, reranker, store, usermerge
from app.jobsources import JobPosting


def _posting(ext, title="Software Engineer", source="greenhouse"):
    return JobPosting(source=source, external_id=ext, title=title, url="https://x",
                      company="Acme", location="Remote", description="Build software.")


def _seed(uid):
    profile.set_profile(uid, roles="software engineer")
    applicant.set_identity(uid, {"email": f"{uid}@x.com"})
    store.create_application(uid, "Stripe", "Backend Engineer", status="Applied")
    jobstore.save_posting(uid, _posting("1"), relevance_score=0.7, status="queued")


def test_merge_moves_all_user_scoped_rows():
    _seed("local")
    moved = usermerge.merge_user("local", "U123")

    # Everything seeded moved to the new id.
    assert "applications" in moved and "job_search_profile" in moved
    assert "job_postings" in moved and "applications" in moved
    assert profile.get_profile("U123")["roles"] == "software engineer"
    assert applicant.get_identity("U123")["email"] == "local@x.com"
    assert len(store.list_applications("U123")) == 1
    assert len(jobstore.list_postings("U123", statuses=("queued",))) == 1
    # Source is now empty of those rows.
    assert profile.get_profile("local") is None
    assert store.list_applications("local") == []


def test_merge_repoints_trained_model_and_labels():
    # Build a trained re-ranker under 'local'.
    for i in range(6):
        jobstore.save_posting("local", _posting(f"p{i}"), relevance_score=0.6, status="applied")
    for i in range(6):
        jobstore.save_posting("local", _posting(f"n{i}", source="rss"),
                              relevance_score=0.3, status="dismissed")
    profile.set_profile("local", roles="software engineer")
    assert reranker.train("local", profile.get_profile("local")) is not None

    usermerge.merge_user("local", "U123")
    assert reranker.load_model("U123") is not None       # model followed
    assert reranker.load_model("local") is None


def test_dry_run_reports_without_moving():
    _seed("local")
    preview = usermerge.merge_user("local", "U123", dry_run=True)
    assert preview["applications"] == 1
    # Nothing actually moved.
    assert profile.get_profile("local") is not None
    assert profile.get_profile("U123") is None


def test_merge_skips_rows_that_would_collide():
    # Both ids already have a profile (single-row-per-user). The merge must keep
    # the destination's row rather than error or overwrite.
    profile.set_profile("local", roles="from local")
    profile.set_profile("U123", roles="from dest")
    usermerge.merge_user("local", "U123")
    assert profile.get_profile("U123")["roles"] == "from dest"   # dst preserved


def test_same_id_is_noop():
    _seed("local")
    assert usermerge.merge_user("local", "local") == {}


# ---------------------------------------------------------------------------
# Cross-database export / import (local trained brain -> a separate "prod" DB)
# ---------------------------------------------------------------------------

def _train_local():
    """Build a profile + 6/6 swipe labels + a trained model under 'local'."""
    from app.db import connect

    profile.set_profile("local", roles="software engineer")
    applicant.set_identity("local", {"email": "ada@x.com"})
    with connect() as conn:
        for i in range(6):
            conn.execute(
                """
                INSERT OR IGNORE INTO training_labels
                    (user_id, source, external_id, company, title, location, url,
                     description, relevance_score, label, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                ("local", "greenhouse", f"p{i}", None, "Software Engineer",
                 None, None, None, 0.6, "like"),
            )
        for i in range(6):
            conn.execute(
                """
                INSERT OR IGNORE INTO training_labels
                    (user_id, source, external_id, company, title, location, url,
                     description, relevance_score, label, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                ("local", "rss", f"n{i}", None, "Sales Rep",
                 None, None, None, 0.2, "pass"),
            )
    assert reranker.train("local", profile.get_profile("local")) is not None


def test_export_import_moves_trained_brain_to_a_fresh_db(tmp_path):
    import sqlite3
    from app.db import SCHEMA, _migrate_schema

    _train_local()
    brain = str(tmp_path / "brain.db")
    exported = usermerge.export_user("local", brain)
    counts = exported.counts
    assert counts["training_labels"] == 12 and counts["reranker_models"] == 1
    assert counts["job_search_profile"] == 1
    assert exported.complete, exported.skipped_tables

    # A separate, schema-complete "production" DB.
    prod = str(tmp_path / "prod.db")
    pc = sqlite3.connect(prod)
    pc.row_factory = sqlite3.Row
    pc.executescript(SCHEMA)
    _migrate_schema(pc)

    imported = usermerge.import_user(brain, "U07LVJVD4PL", conn=pc)
    added = imported.counts
    assert added["training_labels"] == 12 and added["reranker_models"] == 1
    assert imported.complete, imported.skipped_tables

    # The model, profile, identity, and labels all landed under the target id.
    model = pc.execute("SELECT model_json FROM reranker_models WHERE user_id = ?",
                       ("U07LVJVD4PL",)).fetchone()
    assert model is not None
    prof = pc.execute("SELECT roles, applicant_json FROM job_search_profile WHERE user_id = ?",
                      ("U07LVJVD4PL",)).fetchone()
    assert prof["roles"] == "software engineer" and "ada@x.com" in prof["applicant_json"]
    n = pc.execute("SELECT COUNT(*) FROM training_labels WHERE user_id = ?",
                   ("U07LVJVD4PL",)).fetchone()[0]
    assert n == 12


def test_import_is_idempotent_and_nondestructive(tmp_path):
    import sqlite3
    from app.db import SCHEMA, _migrate_schema

    _train_local()
    brain = str(tmp_path / "brain.db")
    usermerge.export_user("local", brain)

    prod = str(tmp_path / "prod.db")
    pc = sqlite3.connect(prod)
    pc.row_factory = sqlite3.Row
    pc.executescript(SCHEMA)
    _migrate_schema(pc)
    # Destination already has its own profile — import must not clobber it.
    pc.execute("INSERT INTO job_search_profile (user_id, roles, updated_at) VALUES (?,?,?)",
               ("U07LVJVD4PL", "existing prod roles", "now"))

    usermerge.import_user(brain, "U07LVJVD4PL", conn=pc)
    usermerge.import_user(brain, "U07LVJVD4PL", conn=pc)  # twice — no dupes

    assert pc.execute("SELECT COUNT(*) FROM training_labels WHERE user_id = ?",
                      ("U07LVJVD4PL",)).fetchone()[0] == 12   # not 24
    # Existing prod profile preserved (INSERT OR IGNORE kept it).
    assert pc.execute("SELECT roles FROM job_search_profile WHERE user_id = ?",
                      ("U07LVJVD4PL",)).fetchone()["roles"] == "existing prod roles"
