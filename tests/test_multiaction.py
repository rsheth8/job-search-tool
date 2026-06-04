"""Multi-action handling for combined SMS messages.

The heuristic router never splits, so we inject a fake router that returns
multiple actions (mimicking what Claude does for a combined message).
"""
from app import store
from app.context import get_context
from app.conversation import get_pending
from app.engine import handle_sms
from app.intents import Intent, ParsedMessage

U = "+15553330000"


def _pm(intent, **kw):
    kw.setdefault("confidence", 0.9)
    return ParsedMessage(intent=intent, **kw)


class FakeRouter:
    name = "fake"

    def __init__(self, actions):
        self._actions = actions

    def parse_actions(self, text):
        return list(self._actions)

    def parse(self, text):
        return self._actions[0]


def _use(monkeypatch, actions):
    monkeypatch.setattr("app.engine.get_router", lambda: FakeRouter(actions))


def test_two_applications_both_logged(monkeypatch):
    _use(monkeypatch, [
        _pm(Intent.APPLY, company="Notion", role="PM"),
        _pm(Intent.APPLY, company="Airtable", role="PM"),
    ])
    reply = handle_sms(U, "applied to notion and airtable, both pm roles")
    assert "Notion" in reply and "Airtable" in reply
    assert store.find_application(U, "Notion") is not None
    assert store.find_application(U, "Airtable") is not None
    # Context ends on the last action.
    assert get_context(U)["last_company"] == "Airtable"


def test_update_plus_apply(monkeypatch):
    # Pre-existing Stripe application to update.
    store.create_application(U, "Stripe", "Backend", status="Applied")
    _use(monkeypatch, [
        _pm(Intent.UPDATE, company="Stripe", status="OA received"),
        _pm(Intent.APPLY, company="Ramp"),
    ])
    reply = handle_sms(U, "got the oa from stripe and also applied to ramp")
    assert "OA received" in reply and "Ramp" in reply
    assert store.find_application(U, "Stripe")["status"] == "OA received"
    assert store.find_application(U, "Ramp") is not None


def test_note_resolves_company_from_earlier_action(monkeypatch):
    # Second action has no company; it should resolve to the just-applied one.
    _use(monkeypatch, [
        _pm(Intent.APPLY, company="Vercel", role="DevRel"),
        _pm(Intent.NOTE, company=None, message="referred by a friend"),
    ])
    handle_sms(U, "applied to vercel devrel, note: referred by a friend")
    app = store.find_application(U, "Vercel")
    with store.connect() as conn:
        notes = conn.execute(
            "SELECT content FROM application_events WHERE application_id=? AND type='note'",
            (app["id"],),
        ).fetchall()
    assert any("referred" in n["content"] for n in notes)


def test_deferred_question_stops_remaining(monkeypatch):
    # First action is an incomplete update (no status) -> asks a question and
    # the second action is deferred; pending is left active.
    _use(monkeypatch, [
        _pm(Intent.UPDATE, company="Spotify", status=None, confidence=0.5),
        _pm(Intent.APPLY, company="Ramp"),
    ])
    reply = handle_sms(U, "spotify update and applied to ramp")
    assert "update spotify to" in reply.lower()
    assert get_pending(U).active            # waiting on the status answer
    assert store.find_application(U, "Ramp") is None  # deferred, not logged
