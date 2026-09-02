"""Timing an application: open form -> Filed.

The plan's north star was "under three minutes, open form to Filed, eight times
in an evening" — a number the product did not record, which made Phase 1's exit
condition unfalsifiable. These tests pin the measurement's semantics, because a
metric that is subtly wrong is worse than none: it would be trusted.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import clock, jobstore
from app.jobsources import JobPosting

_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _save(user="u1", ext="1") -> int:
    row = jobstore.save_posting(
        user,
        JobPosting(source="greenhouse", external_id=ext, title="Engineer",
                   url="https://x/apply", company="Acme", location="Remote",
                   description="Build software."),
        relevance_score=0.7, status="queued",
    )
    return row["id"]


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _at(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes_ago)).strftime(_FMT)


def _lap(user: str, pid: int, *, opened: float, filled: float | None,
         filed: float) -> None:
    """One application, timed by minutes-ago so tests read like a stopwatch."""
    clock.mark(user, pid, "opened", at=_at(opened))
    if filled is not None:
        clock.mark(user, pid, "filled", at=_at(filled))
    clock.mark(user, pid, "filed", at=_at(filed))


# --- the marks -------------------------------------------------------------

def test_filing_records_the_stop_server_side(client):
    """The client never sends `filed`; /apply/applied writes it."""
    pid = _save()
    client.post("/apply/clock",
                json={"user": "u1", "posting_id": pid, "mark": "opened"})
    client.post("/apply/applied", json={"user": "u1", "posting_id": pid})

    rows = clock.sessions("u1")
    assert len(rows) == 1
    assert rows[0]["posting_id"] == pid


def test_a_client_cannot_post_itself_a_finish_line(client):
    """`filed` decides whether a lap counts, so it is not a client mark."""
    pid = _save()
    r = client.post("/apply/clock",
                    json={"user": "u1", "posting_id": pid, "mark": "filed"})
    assert r.json()["ok"] is False
    assert clock.sessions("u1") == []


def test_an_unknown_mark_is_ignored_not_an_error(client):
    """Measurement must never be why an application fails."""
    pid = _save()
    r = client.post("/apply/clock",
                    json={"user": "u1", "posting_id": pid, "mark": "banana"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_tapping_filed_twice_stamps_one_stop(client):
    """Duplicate Filed is the same application, so it stops the clock once.

    Asserting the session count here would prove nothing — sessions are grouped
    per posting and anchored on the *first* `filed`, so a stray second mark
    could never produce a second lap anyway. The guard is what keeps the table
    honest, so count the marks.
    """
    from app.db import connect

    pid = _save()
    client.post("/apply/clock",
                json={"user": "u1", "posting_id": pid, "mark": "opened"})
    client.post("/apply/applied", json={"user": "u1", "posting_id": pid})
    client.post("/apply/applied", json={"user": "u1", "posting_id": pid})

    with connect() as c:
        stops = c.execute(
            "SELECT COUNT(*) FROM apply_marks WHERE posting_id = ? "
            "AND mark = 'filed'", (pid,)).fetchone()[0]
    assert stops == 1, "the second tap on Filed stamped another stop"


def test_a_late_second_stop_does_not_stretch_the_lap():
    """If a stray stop ever does land, the lap is still to the first one.

    Someone taps Filed, keeps the form open, taps again ten minutes later. The
    application took two minutes; anchoring on the last mark would report
    twelve and quietly fail a target that was met.
    """
    pid = _save()
    clock.mark("u1", pid, "opened", at=_at(12))
    clock.mark("u1", pid, "filed", at=_at(10))
    clock.mark("u1", pid, "filed", at=_at(0))

    row = clock.sessions("u1")[0]
    assert 110 <= row["open_to_filed"] <= 130, row["open_to_filed"]


# --- what a session actually means -----------------------------------------

def test_the_lap_is_measured_from_the_open_that_finished_it():
    """Opened, wandered off, came back, finished in two minutes.

    Measuring from the *first* open would report 62 minutes and put a fast
    application over target. The finishing attempt is the lap; the abandoned
    one is recorded separately as a reopen.
    """
    pid = _save()
    clock.mark("u1", pid, "opened", at=_at(64))
    clock.mark("u1", pid, "opened", at=_at(2))
    clock.mark("u1", pid, "filed", at=_at(0))

    row = clock.sessions("u1")[0]
    assert 110 <= row["open_to_filed"] <= 130, row["open_to_filed"]
    assert row["reopens"] == 1


def test_an_application_filed_without_opening_is_not_timed():
    """Filed from chat, or from a build too old to send marks.

    It must be left out, not counted as a zero-second application — that would
    be the single fastest way to make the median look like a win.
    """
    pid = _save()
    clock.mark("u1", pid, "filed", at=_at(0))
    assert clock.sessions("u1") == []
    assert clock.summary("u1")["timed"] == 0


def test_a_perfect_fill_still_times_the_fill_leg():
    """A fill with zero skips reports no skips — but must still mark `filled`.

    The client's skip report bails early when there is nothing to report, so
    hanging the fill mark off it would have silently dropped exactly the
    cleanest, fastest fills and biased the leg medians the wrong way.
    """
    pid = _save()
    _lap("u1", pid, opened=3, filled=2, filed=0)
    row = clock.sessions("u1")[0]
    assert row["open_to_fill"] is not None
    assert row["fill_to_filed"] is not None


def test_the_legs_add_up_to_the_lap():
    pid = _save()
    _lap("u1", pid, opened=5, filled=3, filed=0)
    row = clock.sessions("u1")[0]
    assert row["open_to_fill"] + row["fill_to_filed"] == row["open_to_filed"]


# --- the summary -----------------------------------------------------------

def test_under_target_counts_the_laps_that_beat_three_minutes():
    for i, minutes in enumerate([1, 2, 2.5, 4, 9]):
        _lap("u1", _save(ext=str(i)), opened=minutes, filled=None, filed=0)
    s = clock.summary("u1")
    assert s["timed"] == 5
    assert s["target_seconds"] == 180
    assert s["under_target"] == 3          # 1:00, 2:00, 2:30
    assert s["fastest_seconds"] <= 70


def test_one_abandoned_application_cannot_hide_a_good_evening():
    """The reason this reports a median and not a mean.

    Eight fast laps and one left open overnight: the mean is over ten minutes
    and says the target was missed; the median says a typical application took
    two, which is the true answer to the question being asked.
    """
    for i in range(8):
        _lap("u1", _save(ext=f"fast{i}"), opened=2, filled=None, filed=0)
    _lap("u1", _save(ext="stale"), opened=600, filled=None, filed=0)

    s = clock.summary("u1")
    mean = sum(r["open_to_filed"] for r in clock.sessions("u1")) / s["timed"]
    assert mean > 3600, "the outlier really is that big"
    assert s["median_seconds"] <= 180
    assert s["under_target"] == 8


def test_best_sitting_reports_speed_next_to_count():
    """Eight slow files is a different evening from eight fast ones."""
    for i in range(8):
        _lap("u1", _save(ext=f"n{i}"), opened=2, filled=None, filed=0)
    best = clock.best_sitting("u1")
    assert best["filed"] == 8
    assert best["median_seconds"] <= 180


def test_the_window_keeps_a_lap_that_straddles_it():
    """A form opened before the cutoff and filed inside it is still one lap.

    Filtering marks by date in SQL would drop the `opened` row and silently
    reclassify a timed application as untimed.
    """
    pid = _save()
    clock.mark("u1", pid, "opened", at=_at(60 * 24 + 3))   # just over a day ago
    clock.mark("u1", pid, "filed", at=_at(60 * 24 - 1))    # just under
    rows = clock.sessions("u1", days=2)
    assert len(rows) == 1
    assert rows[0]["open_to_filed"] > 0


def test_timings_endpoint_reports_the_gate(client):
    pid = _save()
    _lap("u1", pid, opened=2, filled=1, filed=0)
    body = client.get("/apply/timings?user=u1").json()
    assert body["summary"]["timed"] == 1
    assert body["summary"]["under_target"] == 1
    assert body["sessions"][0]["posting_id"] == pid


def test_one_users_laps_do_not_appear_in_anothers():
    _lap("u1", _save(user="u1", ext="a"), opened=2, filled=None, filed=0)
    assert clock.summary("u2")["timed"] == 0
