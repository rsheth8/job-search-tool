from app import store
from app.context import get_context
from app.engine import handle_sms

U = "+15550001111"


def test_apply_creates_application_and_context():
    reply = handle_sms(U, "applied spotify swe ii")
    assert "Spotify" in reply and "Applied" in reply

    apps = store.list_applications(U)
    assert len(apps) == 1
    assert apps[0]["company"] == "Spotify"

    mem = get_context(U)
    assert mem["last_company"] == "Spotify"
    assert mem["last_application_id"] == apps[0]["id"]


def test_apply_then_context_resolves_update():
    handle_sms(U, "applied spotify swe ii")
    # "spotify update oa received" -> status update on existing row
    reply = handle_sms(U, "spotify update oa received")
    assert "OA received" in reply
    assert store.find_application(U, "Spotify")["status"] == "OA received"


def test_update_without_status_asks_question():
    handle_sms(U, "applied spotify swe ii")
    reply = handle_sms(U, "spotify update")
    assert "update Spotify to" in reply.lower() or "what should i update" in reply.lower()


def test_note_attaches_to_last_application():
    handle_sms(U, "applied figma backend engineer")
    reply = handle_sms(U, "note figma recruiter was great")
    assert "Figma" in reply
    app = store.find_application(U, "Figma")
    with store.connect() as conn:
        notes = conn.execute(
            "SELECT * FROM application_events WHERE application_id=? AND type='note'",
            (app["id"],),
        ).fetchall()
    assert len(notes) == 1


def test_bare_apply_asks_for_company():
    reply = handle_sms(U, "applied")
    assert "which company" in reply.lower()


def test_update_unknown_company_with_status_creates_it():
    reply = handle_sms(U, "stripe interview")
    assert "Stripe" in reply
    assert store.find_application(U, "Stripe") is not None


def test_list():
    handle_sms(U, "applied spotify swe ii")
    handle_sms(U, "applied figma backend engineer")
    reply = handle_sms(U, "list applied")
    assert "Spotify" in reply and "Figma" in reply


def test_query_surfaces_open_apps():
    handle_sms(U, "applied spotify swe ii")
    reply = handle_sms(U, "what should I follow up on")
    assert "Spotify" in reply


def test_context_isolated_per_user():
    handle_sms("userA", "applied spotify swe ii")
    handle_sms("userB", "applied figma backend engineer")
    assert get_context("userA")["last_company"] == "Spotify"
    assert get_context("userB")["last_company"] == "Figma"
