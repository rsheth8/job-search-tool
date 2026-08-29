"""Skipped Fill labels become a phrasing table from real applications."""
from __future__ import annotations

from app import filllearn


def test_record_and_list_skips_dedupes_and_counts():
    uid = "filllearn-u1"
    filllearn.record_skips(
        uid,
        [
            {"label": "Favorite color", "reason": "unmatched"},
            {"label": "Favorite  color", "reason": "unmatched"},
            {"label": "School", "reason": "empty", "key": "school"},
            {"label": "Country", "reason": "no_option", "key": "country",
             "options": ["Mars", "Venus"]},
        ],
        url="https://boards.greenhouse.io/acme/jobs/1",
        posting_id=9,
    )
    rows = {(r["label"].lower(), r["reason"]): r for r in filllearn.list_skips(uid)}
    color = rows[("favorite color", "unmatched")]
    assert color["count"] == 1  # same request deduped before insert
    assert color["url"].endswith("/jobs/1")
    assert rows[("school", "empty")]["key"] == "school"
    assert rows[("country", "no_option")]["options"] == ["Mars", "Venus"]

    filllearn.record_skips(
        uid, [{"label": "Favorite color", "reason": "unmatched"}],
        url="https://jobs.ashbyhq.com/acme",
    )
    again = next(r for r in filllearn.list_skips(uid)
                 if r["reason"] == "unmatched")
    assert again["count"] == 2
    assert "ashbyhq" in again["url"]


def test_ignores_short_and_unknown_reasons():
    filllearn.record_skips("filllearn-u2", [
        {"label": "Hi", "reason": "unmatched"},
        {"label": "Favorite color", "reason": "mystery"},
        {"label": "   ", "reason": "unmatched"},
        "not a dict",
    ])
    assert filllearn.list_skips("filllearn-u2") == []


def test_fill_skips_endpoint_round_trip():
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    body = client.post("/apply/fill-skips", json={
        "user": "filllearn-u3",
        "url": "https://example.com/apply",
        "posting_id": 4,
        "skips": [
            {"label": "What's your favorite snack?", "reason": "unmatched"},
            {"label": "School / University", "reason": "empty", "key": "school"},
        ],
    }).json()
    assert body["stored"] == 2
    listed = client.get("/apply/fill-skips?user=filllearn-u3").json()["skips"]
    labels = {r["label"] for r in listed}
    assert "What's your favorite snack?" in labels
    assert "School / University" in labels
