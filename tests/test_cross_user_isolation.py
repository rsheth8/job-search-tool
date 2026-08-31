"""One user must not be able to reach another's rows through an id.

Application and posting ids are a shared AUTOINCREMENT sequence: user A's first
application is id 1, and so is nobody else's. So an id on its own says nothing
about who owns it, and `WHERE id = ?` reads or writes whatever row holds that
number.

Every HTTP endpoint already looked the row up scoped before touching it, so there
was no live hole. But that made isolation a convention each call site had to
remember — nineteen of them — and the store would have happily served a request
that forgot. These tests are written against the store directly, underneath the
endpoints, so they fail if the guarantee ever moves back up a layer.

Every test here passes trivially if you delete the assertion; what makes them
worth having is that each one FAILS against the previous version of the code,
where the user id was not a parameter at all.
"""
from __future__ import annotations

import pytest

from app import jobstore, store
from app.db import connect

ALICE, MALLORY = "usr_alice", "usr_mallory"


@pytest.fixture
def app_of_alice():
    """One application owned by Alice, plus a note so it has an event."""
    row = store.create_application(ALICE, "Stripe", "Engineer")
    store.add_note(ALICE, row["id"], "spoke to their recruiter")
    return row


@pytest.fixture
def posting_of_alice():
    return jobstore.save_posting(ALICE, jobstore.JobPosting(
        source="greenhouse", external_id="e1", company="Stripe",
        title="Engineer", location="Chicago", url="http://x/1", description=""))


# --- reads ------------------------------------------------------------------

def test_a_stranger_cannot_read_an_application(app_of_alice):
    assert store.get_application(ALICE, app_of_alice["id"]) is not None
    assert store.get_application(MALLORY, app_of_alice["id"]) is None


def test_a_stranger_cannot_read_the_event_timeline(app_of_alice):
    """`application_events` has no user_id of its own — ownership is on the
    parent, so this only holds if the query joins through it."""
    assert store.list_events(ALICE, app_of_alice["id"]) != []
    assert store.list_events(MALLORY, app_of_alice["id"]) == []


def test_a_stranger_cannot_find_the_last_event_id(app_of_alice):
    assert store.last_event_id(ALICE, app_of_alice["id"], "note") is not None
    assert store.last_event_id(MALLORY, app_of_alice["id"], "note") is None


def test_a_stranger_does_not_see_the_recruiter_signal(app_of_alice):
    assert store.has_recruiter_signal(ALICE, app_of_alice["id"]) is True
    assert store.has_recruiter_signal(MALLORY, app_of_alice["id"]) is False


# --- writes -----------------------------------------------------------------

def test_a_stranger_cannot_change_the_status(app_of_alice):
    assert store.update_status(MALLORY, app_of_alice["id"], "Rejected") is None
    assert store.get_application(ALICE, app_of_alice["id"])["status"] == "Applied"


def test_a_stranger_cannot_attach_a_note(app_of_alice):
    before = len(store.list_events(ALICE, app_of_alice["id"]))
    assert store.add_note(MALLORY, app_of_alice["id"], "injected") is False
    assert len(store.list_events(ALICE, app_of_alice["id"])) == before


def test_a_stranger_cannot_edit_the_fields(app_of_alice):
    assert store.edit_application(MALLORY, app_of_alice["id"],
                                  company="Mallory Corp") is None
    assert store.get_application(ALICE, app_of_alice["id"])["company"] == "Stripe"


def test_a_stranger_cannot_delete_the_application(app_of_alice):
    assert store.delete_application(MALLORY, app_of_alice["id"]) is False
    assert store.get_application(ALICE, app_of_alice["id"]) is not None
    assert store.delete_application(ALICE, app_of_alice["id"]) is True
    assert store.get_application(ALICE, app_of_alice["id"]) is None


def test_a_stranger_cannot_delete_an_event(app_of_alice):
    eid = store.last_event_id(ALICE, app_of_alice["id"], "note")
    assert store.delete_event(MALLORY, eid) is False
    assert store.last_event_id(ALICE, app_of_alice["id"], "note") == eid
    assert store.delete_event(ALICE, eid) is True


def test_a_stranger_cannot_restore_over_it(app_of_alice):
    """Undo writes prior values back verbatim — a fine way to overwrite someone
    else's row if it is not scoped."""
    assert store.restore_application(
        MALLORY, app_of_alice["id"], {"company": "Mallory Corp"}) is False
    assert store.get_application(ALICE, app_of_alice["id"])["company"] == "Stripe"


# --- postings ---------------------------------------------------------------

def test_a_stranger_cannot_change_a_posting_status(posting_of_alice):
    pid = posting_of_alice["id"]
    assert jobstore.mark_posting_status(MALLORY, pid, "dismissed") is False
    assert jobstore.get_posting(ALICE, pid)["status"] != "dismissed"
    assert jobstore.mark_posting_status(ALICE, pid, "dismissed") is True


def test_a_stranger_cannot_snooze_a_posting(posting_of_alice):
    pid = posting_of_alice["id"]
    assert jobstore.snooze_posting(MALLORY, pid, "2030-01-01T00:00:00Z") is False
    assert jobstore.get_posting(ALICE, pid)["status"] != "snoozed"


# --- the ids really do collide ----------------------------------------------

def test_two_users_first_rows_do_not_share_an_id():
    """Guards the premise. If ids were unique per user the rest would be moot —
    they are not; they are a single sequence, so id 1 exists exactly once and
    belongs to whoever got there first."""
    a = store.create_application(ALICE, "Stripe", "Engineer")
    m = store.create_application(MALLORY, "Acme", "Analyst")
    assert a["id"] != m["id"]
    # Mallory reaching for Alice's number gets nothing, not her own row.
    assert store.get_application(MALLORY, a["id"]) is None


def test_the_guard_is_the_where_clause_not_a_lookup(app_of_alice):
    """update_status must not read-then-write: the ownership test belongs in the
    statement that does the writing, or there is a window between them."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM applications WHERE id = ? AND user_id = ?",
            (app_of_alice["id"], MALLORY)).fetchone()[0]
    assert rows == 0
    assert store.update_status(MALLORY, app_of_alice["id"], "Offer") is None
