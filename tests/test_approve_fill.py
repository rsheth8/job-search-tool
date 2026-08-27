"""Approving a filled application from Slack, and the in-flight status view.

The submit pipeline's human gate used to live only on the /apply web page, which
meant the phone flow (alert → queue → fill → approve) had to leave chat. These
cover the conversational half: routing "approve"/"cancel", the state transitions
they drive, and the "what's in flight" summary.

The invariant under test: **only an explicit human approval moves a request to
`approved`** — nothing else in the pipeline may.
"""
from __future__ import annotations

import pytest

from app import engine, fill_requests, jobstore, router
from app.intents import Intent
from app.jobsources import JobPosting


def _posting(user_id="u1", company="Acme", title="Backend Engineer"):
    return jobstore.save_posting(user_id, JobPosting(
        source="greenhouse", external_id=f"{company}-1", title=title,
        url="https://boards.greenhouse.io/acme/jobs/1", company=company,
        location="Remote", description="Build backend services."),
        relevance_score=0.8, status="queued")


def _at_preview(user_id="u1"):
    """A fill request sitting at `preview`, i.e. awaiting the human."""
    posting = _posting(user_id)
    req = fill_requests.create(user_id, posting["id"])
    fill_requests.claim_next()
    fill_requests.set_preview(req["id"], {"filled": [{"label": "Email", "value": "a@b.c"}],
                                          "skipped": ["Gender"]})
    return posting, fill_requests.get(req["id"])


# --- routing ----------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "approve", "Approve", "submit it", "send it", "ship it", "go ahead",
    "looks good", "lgtm", "approve 7", "approve #7", "approve the application",
])
def test_approve_phrases_route_to_approve_fill(text):
    p = router.HeuristicRouter().parse(text)
    assert p.intent == Intent.APPROVE_FILL
    assert p.message.startswith("approve")


@pytest.mark.parametrize("text", [
    "cancel", "cancel it", "abort", "don't submit", "dont submit it",
    "stop", "cancel #7", "cancel the fill",
])
def test_cancel_phrases_route_to_approve_fill(text):
    p = router.HeuristicRouter().parse(text)
    assert p.intent == Intent.APPROVE_FILL
    assert p.message.startswith("cancel")


def test_approve_carries_the_posting_number():
    assert router.HeuristicRouter().parse("approve #12").message == "approve:12"
    assert router.HeuristicRouter().parse("cancel 12").message == "cancel:12"


@pytest.mark.parametrize("text,intent", [
    # these share words with approve/cancel but mean something else entirely
    ("applied to Stripe", Intent.APPLY),
    ("update Acme status: rejected", Intent.UPDATE),
    ("what did I apply to this week", Intent.LIST),
])
def test_approve_patterns_do_not_swallow_other_intents(text, intent):
    assert router.HeuristicRouter().parse(text).intent == intent


@pytest.mark.parametrize("text", [
    "what's in flight", "in flight", "application status", "what's pending",
    "pending approvals", "status of my applications", "waiting on my approval",
])
def test_status_phrases_route_to_apply_status(text):
    assert router.HeuristicRouter().parse(text).intent == Intent.APPLY_STATUS


# --- the gate ---------------------------------------------------------------

def test_approve_moves_preview_to_approved():
    posting, req = _at_preview()
    reply = engine.handle_sms("u1", "approve")
    assert "approved" in reply.lower()
    assert fill_requests.get(req["id"])["status"] == fill_requests.APPROVED


def test_cancel_kills_the_request_without_submitting():
    posting, req = _at_preview()
    reply = engine.handle_sms("u1", "cancel")
    assert "cancelled" in reply.lower()
    after = fill_requests.get(req["id"])
    assert after["status"] == fill_requests.FAILED
    assert after["error"] == "cancelled"


def test_approve_by_posting_number():
    posting, req = _at_preview()
    engine.handle_sms("u1", f"approve #{posting['id']}")
    assert fill_requests.get(req["id"])["status"] == fill_requests.APPROVED


def test_approve_with_nothing_waiting_says_so():
    reply = engine.handle_sms("u1", "approve")
    assert "nothing is waiting" in reply.lower()


def test_approve_before_the_preview_is_ready_does_not_approve():
    """A request still being filled has nothing for the human to judge yet — it
    must not skip the gate just because they typed 'approve' early."""
    posting = _posting()
    req = fill_requests.create("u1", posting["id"])
    fill_requests.claim_next()          # -> filling, no preview yet
    reply = engine.handle_sms("u1", "approve")
    assert fill_requests.get(req["id"])["status"] == fill_requests.FILLING
    assert "isn't ready" in reply.lower()


def test_approve_is_scoped_to_the_owner():
    posting, req = _at_preview("u1")
    engine.handle_sms("u2", "approve")   # a different user tries to approve
    assert fill_requests.get(req["id"])["status"] == fill_requests.PREVIEW


def test_approving_an_already_submitted_request_is_a_no_op():
    posting, req = _at_preview()
    fill_requests.approve("u1", req["id"])
    fill_requests.claim_approved()
    fill_requests.mark_submitted(req["id"])
    reply = engine.handle_sms("u1", "approve")
    assert "already" in reply.lower()
    assert fill_requests.get(req["id"])["status"] == fill_requests.SUBMITTED


# --- in-flight status -------------------------------------------------------

def test_status_lists_what_is_waiting_on_you():
    posting, _req = _at_preview()
    reply = engine.handle_sms("u1", "what's in flight")
    assert "in flight" in reply.lower()
    assert f"#{posting['id']}" in reply
    assert "waiting on your approval" in reply.lower()
    assert "approve" in reply.lower()


def test_status_reports_each_state():
    posting = _posting()
    req = fill_requests.create("u1", posting["id"])
    assert "queued for the worker" in engine.handle_sms("u1", "in flight").lower()
    fill_requests.claim_next()
    assert "filling the form" in engine.handle_sms("u1", "in flight").lower()
    fill_requests.set_preview(req["id"], {"filled": [], "skipped": []})
    assert "waiting on your approval" in engine.handle_sms("u1", "in flight").lower()


def test_status_when_idle_points_at_the_next_step():
    reply = engine.handle_sms("u1", "what's in flight")
    assert "nothing in flight" in reply.lower()


def test_finished_requests_drop_out_of_in_flight():
    posting, req = _at_preview()
    fill_requests.cancel("u1", req["id"])
    assert "nothing in flight" in engine.handle_sms("u1", "in flight").lower()


# --- the preview notification ----------------------------------------------

def test_preview_message_shows_fills_skips_and_the_two_words():
    posting, req = _at_preview()
    msg = engine.fill_preview_message("u1", req, {
        "filled": [{"label": "Email", "value": "a@b.c"},
                   {"label": "First Name", "value": "Rahil"}],
        "skipped": ["Gender", "Race"],
    })
    assert "Email" in msg and "First Name" in msg
    assert "Left for you" in msg and "Gender" in msg
    assert "approve" in msg and "cancel" in msg


def test_dashboard_in_flight_matches_the_slack_view():
    """Phone and computer read the same rows — they can't disagree about state."""
    from app import dashboard

    posting, req = _at_preview()
    rows = dashboard.in_flight_rows("u1")
    assert [r["id"] for r in rows] == [posting["id"]]
    assert rows[0]["awaiting"] is True
    assert rows[0]["state"] == "waiting on your approval"
    assert "Backend Engineer @ Acme" in rows[0]["label"]

    html = dashboard.render("u1")
    assert "In flight" in html and "review &amp; approve" in html


def test_dashboard_in_flight_hides_finished_work():
    from app import dashboard

    posting, req = _at_preview()
    fill_requests.cancel("u1", req["id"])
    assert dashboard.in_flight_rows("u1") == []
    assert "In flight" not in dashboard.render("u1")


def test_worker_preview_pushes_the_gate_to_the_user():
    """The worker reporting a filled form must reach the user's phone — otherwise
    the request sits at `preview` forever waiting on a page they never opened."""
    from fastapi.testclient import TestClient

    from app import reminders
    from app.main import app

    posting = _posting()
    req = fill_requests.create("u1", posting["id"])
    fill_requests.claim_next()
    sender = reminders.get_sender()          # AppSender: records + chat transcript

    r = TestClient(app).post("/worker/preview", json={
        "request_id": req["id"],
        "preview": {"filled": [{"label": "Email", "value": "a@b.c"}], "skipped": []}})

    assert r.json()["ok"] is True
    assert fill_requests.get(req["id"])["status"] == fill_requests.PREVIEW
    sent = [body for uid, body in sender.sent if uid == "u1"]
    assert sent and "approve" in sent[-1]


def test_a_broken_notification_never_strands_the_fill():
    """Messaging is best-effort: if it throws, the preview must still be recorded
    (the /apply page is the fallback)."""
    from fastapi.testclient import TestClient

    from app import main, reminders
    from app.main import app

    posting = _posting()
    req = fill_requests.create("u1", posting["id"])
    fill_requests.claim_next()

    class Broken:
        def send(self, *_a, **_kw):
            raise RuntimeError("slack is down")

    original = reminders.get_sender
    reminders.get_sender = lambda: Broken()
    try:
        r = TestClient(app).post("/worker/preview", json={
            "request_id": req["id"], "preview": {"filled": [], "skipped": []}})
    finally:
        reminders.get_sender = original

    assert r.json()["ok"] is True
    assert fill_requests.get(req["id"])["status"] == fill_requests.PREVIEW


def test_preview_message_for_a_blocked_fill_hands_off():
    posting, req = _at_preview()
    msg = engine.fill_preview_message("u1", req, {
        "filled": [], "skipped": [], "status": "blocked", "reason": "login wall"})
    assert "login wall" in msg
    assert "extension" in msg.lower()
