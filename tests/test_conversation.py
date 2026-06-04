"""Multi-turn conversation behaviors."""
from app import store
from app.context import get_context
from app.conversation import get_pending
from app.engine import handle_sms

U = "+15557770000"


# --- slot filling across turns -------------------------------------------

def test_update_status_filled_on_next_turn():
    handle_sms(U, "applied spotify swe ii")
    ask = handle_sms(U, "spotify update")
    assert "update Spotify to" in ask
    assert get_pending(U).awaiting == "status"

    reply = handle_sms(U, "interview")  # bare answer to the question
    assert "Interview" in reply
    assert store.find_application(U, "Spotify")["status"] == "Interview"
    assert not get_pending(U).active  # pending cleared after completion


def test_note_collected_over_two_turns():
    handle_sms(U, "applied figma backend engineer")
    ask = handle_sms(U, "note figma")
    assert "note for Figma" in ask
    handle_sms(U, "recruiter was super responsive")
    with store.connect() as conn:
        notes = conn.execute(
            "SELECT content FROM application_events WHERE type='note'"
        ).fetchall()
    assert any("responsive" in n["content"] for n in notes)


def test_bare_company_answer_completes_apply():
    # No prior context -> bot asks which company -> bare reply completes it.
    ask = handle_sms(U, "applied")
    assert "which company" in ask.lower()
    reply = handle_sms(U, "datadog")
    assert "Datadog" in reply and "Applied" in reply
    assert store.find_application(U, "Datadog") is not None


# --- confirmations --------------------------------------------------------

def test_apply_from_context_requires_yes():
    handle_sms(U, "applied spotify swe ii")
    ask = handle_sms(U, "applied")  # infer Spotify from context
    assert "Spotify" in ask and "yes/no" in ask
    reply = handle_sms(U, "yes")
    assert "Logged" in reply
    # Two Spotify applications now exist (user confirmed a second one).
    apps = [a for a in store.list_applications(U) if a["company"] == "Spotify"]
    assert len(apps) == 2


def test_duplicate_apply_prompts_confirmation():
    handle_sms(U, "applied stripe backend engineer")
    ask = handle_sms(U, "applied stripe backend engineer")
    assert "already have Stripe" in ask
    reply = handle_sms(U, "no")
    assert "not logging" in reply.lower()
    apps = [a for a in store.list_applications(U) if a["company"] == "Stripe"]
    assert len(apps) == 1  # the 'no' prevented a duplicate


# --- corrections & cancel -------------------------------------------------

def test_correction_changes_company():
    handle_sms(U, "applied")          # asks which company
    handle_sms(U, "spotify")          # logs Spotify... then user corrects via update
    # Now mid-update correction:
    handle_sms(U, "applied figma backend engineer")
    ask = handle_sms(U, "applied")    # infer Figma
    assert "Figma" in ask
    reply = handle_sms(U, "no, google")
    assert "Google" in reply or "google" in reply.lower()
    assert store.find_application(U, "Google") is not None


def test_cancel_clears_pending():
    handle_sms(U, "applied spotify swe ii")
    handle_sms(U, "spotify update")
    assert get_pending(U).active
    reply = handle_sms(U, "nevermind")
    assert "scrapped" in reply.lower()
    assert not get_pending(U).active


# --- context switching ----------------------------------------------------

def test_query_interrupts_pending():
    handle_sms(U, "applied spotify swe ii")
    handle_sms(U, "spotify update")     # now awaiting status
    reply = handle_sms(U, "what should I follow up on")
    assert "Spotify" in reply
    # the half-finished update was abandoned cleanly
    assert not get_pending(U).active


# --- help & greeting ------------------------------------------------------

def test_help():
    reply = handle_sms(U, "help")
    assert "LOG & UPDATE" in reply and "follow up" in reply  # the menu


def test_greeting():
    reply = handle_sms(U, "hey there")
    assert "assistant" in reply.lower()
    assert "LOG & UPDATE" in reply  # greeting now includes the menu
