"""SMS menu / help / greeting (offline)."""
from __future__ import annotations

from app import conversation as convo
from app.engine import MENU, handle_sms


def test_help_returns_menu():
    for phrase in ("menu", "commands"):
        reply = handle_sms("m", phrase)
        assert reply == MENU, phrase


def test_help_is_horizon_overview():
    for phrase in ("help", "what can you do", "get started", "?"):
        reply = handle_sms("m", phrase)
        assert "Horizon" in reply, phrase
        assert "LOG & UPDATE" not in reply, phrase


def test_greeting_includes_menu_and_welcome():
    reply = handle_sms("m", "hey")
    assert "horizon" in reply.lower()
    assert "help" in reply.lower()


def test_menu_covers_each_capability():
    for marker in ("applied stripe", "oa received", "note ",
                   "due friday", "remind me", "list", "follow up", "how am I doing",
                   "coming up"):
        assert marker in MENU, marker


def test_question_mark_inside_query_is_not_help():
    # A real question that merely ends in "?" should NOT be hijacked by help.
    assert convo.is_help("what should I follow up on?") is False
    assert convo.is_help("?") is True


def test_logging_still_works_after_menu_change():
    reply = handle_sms("m", "applied stripe swe")
    assert "Logged" in reply
