"""SMS menu / help / greeting (offline)."""
from __future__ import annotations

from app import conversation as convo
from app.engine import MENU, handle_sms


def test_help_returns_menu():
    for phrase in ("help", "menu", "what can you do", "commands", "?", "get started"):
        reply = handle_sms("m", phrase)
        assert reply == MENU, phrase


def test_greeting_includes_menu_and_welcome():
    reply = handle_sms("m", "hey")
    assert "assistant" in reply.lower()
    assert "LOG & UPDATE" in reply  # menu body is included


def test_menu_covers_each_capability():
    for marker in ("applied stripe", "oa received", "note ", "reach out",
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
