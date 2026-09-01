"""Sitting meter + ranker progress. Offline."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import jobstore, momentum, store
from app.db import connect
from app.engine import handle_sms
from app.jobsources import JobPosting
from app.main import app
from fastapi.testclient import TestClient


def _posting(user, ext, status="queued"):
    return jobstore.save_posting(user, JobPosting(
        source="greenhouse", external_id=ext, title="SWE",
        url=f"https://boards.greenhouse.io/acme/jobs/{ext}", company="Acme",
        location="Remote", description="Build backend services."),
        relevance_score=0.8, status=status)


def test_empty_sitting_is_permission_to_start_then_stop():
    snap = momentum.snapshot("nobody")
    assert snap["filed_today"] == 0
    assert snap["sitting_goal"] == 3
    assert snap["sitting_done"] is False
    assert "sitting" in snap["sitting_line"].lower()
    assert "stop" in snap["sitting_line"].lower()
    assert snap["ranker_on"] is False
    assert "file" in snap["ranker_line"]
    assert "pass" in snap["ranker_line"]


def test_filed_today_counts_local_day_not_utc_yesterday():
    store.create_application("u", "Stripe", "SWE")
    snap = momentum.snapshot("u")
    assert snap["filed_today"] == 1
    assert snap["sitting_line"] == "1 of 3 tonight."
    assert snap["toast"] == "1 of 3 tonight."


def test_yesterday_does_not_count():
    app = store.create_application("u", "OldCo", "SWE")
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with connect() as conn:
        conn.execute("UPDATE applications SET applied_at = ? WHERE id = ?",
                     (old, app["id"]))
    assert momentum.snapshot("u")["filed_today"] == 0


def test_sitting_done_tells_them_to_stop():
    for i in range(3):
        store.create_application("u", f"Co{i}", "SWE")
    snap = momentum.snapshot("u")
    assert snap["sitting_done"] is True
    assert snap["filed_today"] == 3
    assert "enough" in snap["sitting_line"].lower()


def test_over_goal_is_anti_spray_not_a_streak():
    for i in range(5):
        store.create_application("u", f"Co{i}", "SWE")
    snap = momentum.snapshot("u")
    assert "spray" in snap["sitting_line"].lower()


def test_ranker_progress_counts_file_and_pass():
    _posting("u", "a", status="applied")
    _posting("u", "b", status="applied")
    _posting("u", "c", status="dismissed")
    snap = momentum.snapshot("u")
    assert snap["likes"] == 2
    assert snap["passes"] == 1
    assert snap["likes_left"] == 3
    assert snap["passes_left"] == 4
    assert "3 more files" in snap["ranker_line"]
    assert "4 more passes" in snap["ranker_line"]


def test_apply_data_includes_momentum():
    store.create_application("u1", "Acme", "SWE")
    body = TestClient(app).get("/apply/data?user=u1").json()
    assert body["momentum"]["filed_today"] == 1
    assert body["momentum"]["sitting_goal"] == 3


def test_applied_and_pass_return_momentum():
    p = _posting("u1", "9", status="queued")
    c = TestClient(app)
    filed = c.post("/apply/applied",
                   json={"user": "u1", "posting_id": p["id"]}).json()
    assert filed["ok"] is True
    assert filed["momentum"]["filed_today"] >= 1
    p2 = _posting("u1", "10", status="queued")
    passed = c.post("/apply/pass",
                    json={"user": "u1", "posting_id": p2["id"]}).json()
    assert passed["ok"] is True
    assert passed["momentum"]["passes"] >= 1


def test_how_am_i_doing_names_the_sitting():
    store.create_application("su", "Acme", "SWE")
    reply = handle_sms("su", "how am I doing")
    assert "1 of 3 tonight" in reply
    assert "Ranking learns you" in reply
