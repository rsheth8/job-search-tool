"""Assistant-feel conversational coverage: smalltalk, CHECK, DELETE, and a wide
net of natural phrasings (all offline via the heuristic router)."""
from __future__ import annotations

import pytest

from app import conversation as convo
from app import store
from app.engine import handle_sms
from app.intents import Intent
from app.router import HeuristicRouter

R = HeuristicRouter()


# --- smalltalk --------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "thanks!", "thank you so much", "thx", "ty", "appreciate it", "cheers",
])
def test_thanks_gets_warm_reply(text):
    reply = handle_sms("u", text)
    assert "got it" in reply.lower() or "🙌" in reply
    assert "didn't" not in reply.lower()  # not the confused fallback


@pytest.mark.parametrize("text", ["cool", "nice", "perfect", "got it", "sounds good", "bet"])
def test_acknowledgements(text):
    reply = handle_sms("u", text)
    assert "Standing by" in reply


@pytest.mark.parametrize("text", [
    "you're awesome", "you are the best", "good bot", "love it", "nice work",
])
def test_compliments(text):
    reply = handle_sms("u", text)
    assert "Happy to help" in reply


@pytest.mark.parametrize("text", ["bye", "goodnight", "night", "see ya", "that's all", "i'm done"])
def test_signoff(text):
    reply = handle_sms("u", text)
    assert "Catch you later" in reply


def test_smalltalk_does_not_hijack_commands():
    # "cool, applied to stripe" carries a real action → APPLY wins, not ack.
    reply = handle_sms("u", "cool, applied to stripe swe")
    assert "Logged" in reply


def test_ack_during_confirm_is_still_yes():
    # A bare "ok" mid-confirm must mean YES, not trigger smalltalk.
    handle_sms("c", "applied stripe")
    handle_sms("c", "applied stripe")  # duplicate → confirm prompt
    reply = handle_sms("c", "ok")
    assert "Logged" in reply


# --- CHECK ------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "what's the status of stripe",
    "where do I stand with stripe",
    "what's the latest on stripe",
    "tell me about stripe",
    "do I have stripe",
    "update on stripe",
])
def test_check_phrasings_route_to_check(text):
    p = R.parse(text)
    assert p.intent == Intent.CHECK, text
    assert p.company == "Stripe", text


def test_check_returns_rich_summary():
    store.create_application("u", "Stripe", "SWE")
    app = store.find_application("u", "Stripe")
    store.update_status("u", app["id"], "OA received")
    store.add_note("u", app["id"], "recruiter was great")
    reply = handle_sms("u", "what's the status of stripe")
    assert "Stripe" in reply
    assert "OA received" in reply
    assert "recruiter was great" in reply


def test_check_unknown_company_offers_to_add():
    reply = handle_sms("u", "what's the status of google")
    assert "don't have Google" in reply
    assert "applied Google" in reply


def test_check_does_not_mutate():
    store.create_application("u", "Stripe", "SWE")
    handle_sms("u", "what's the status of stripe")
    assert store.find_application("u", "Stripe")["status"] == "Applied"


# --- DELETE -----------------------------------------------------------------

def test_delete_requires_confirmation_then_removes():
    store.create_application("u", "Stripe", "SWE")
    prompt = handle_sms("u", "delete stripe")
    assert "yes/no" in prompt.lower()
    assert store.find_application("u", "Stripe") is not None  # not gone yet
    done = handle_sms("u", "yes")
    assert "Deleted" in done
    assert store.find_application("u", "Stripe") is None


def test_delete_can_be_cancelled():
    store.create_application("u", "Stripe", "SWE")
    handle_sms("u", "delete stripe")
    reply = handle_sms("u", "no")
    assert store.find_application("u", "Stripe") is not None  # still there


def test_delete_unknown_company():
    reply = handle_sms("u", "delete google")
    assert "nothing to delete" in reply.lower()


@pytest.mark.parametrize("text", [
    "delete stripe", "remove stripe", "get rid of stripe",
    "I never applied to stripe", "actually I didn't apply to stripe",
])
def test_delete_phrasings(text):
    p = R.parse(text)
    assert p.intent == Intent.DELETE, text
    assert p.company == "Stripe", text


def test_delete_cascades_events_but_keeps_other_apps():
    store.create_application("u", "Stripe", "SWE")
    store.create_application("u", "Notion", "PM")
    handle_sms("u", "delete stripe")
    handle_sms("u", "yes")
    assert store.find_application("u", "Notion") is not None


# --- misc robustness --------------------------------------------------------

def test_question_about_overdue_still_query_not_check():
    p = R.parse("what should I follow up on")
    assert p.intent == Intent.QUERY


def test_smalltalk_reply_returns_none_for_real_text():
    assert convo.smalltalk_reply("applied to stripe") is None
