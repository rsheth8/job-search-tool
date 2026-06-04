"""Relative-date application queries ("what did I apply to this week"). Offline."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import store
from app.engine import _apply_window, handle_sms
from app.intents import Intent
from app.router import HeuristicRouter

R = HeuristicRouter()


# --- routing ----------------------------------------------------------------

@pytest.mark.parametrize("text,window", [
    ("what did I apply to this week", "this week"),
    ("what have I applied to today", "today"),
    ("anything new since monday", "since monday"),
    ("what did I apply to last month", "last month"),
])
def test_recent_queries_route_to_list_with_window(text, window):
    p = R.parse(text)
    assert p.intent is Intent.LIST
    assert p.time_reference == window


def test_recent_query_is_not_mistaken_for_apply():
    # Contains the word "apply" but must not log an application.
    assert R.parse("what did I apply to this week").intent is Intent.LIST


# --- window math ------------------------------------------------------------

def test_apply_window_today_and_yesterday():
    now = datetime(2026, 5, 30, 15, 0, tzinfo=timezone.utc)  # a Saturday
    start, end = _apply_window("today", now)
    assert start == datetime(2026, 5, 30, tzinfo=timezone.utc)
    assert end is None
    start, end = _apply_window("yesterday", now)
    assert start == datetime(2026, 5, 29, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 30, tzinfo=timezone.utc)


def test_apply_window_unrecognized_returns_none():
    assert _apply_window("whenever", datetime.now(timezone.utc)) is None


# --- end to end -------------------------------------------------------------

def test_store_window_filters_by_applied_at():
    now = datetime.now(timezone.utc)
    store.create_application("u", "Fresh", "SWE")  # applied now
    store.create_application(
        "u", "Old", "PM", applied_at=now - timedelta(days=40)
    )
    week = store.applications_in_window("u", now - timedelta(days=7))
    names = {a["company"] for a in week}
    assert "Fresh" in names and "Old" not in names


def test_this_week_query_excludes_older_apps():
    now = datetime.now(timezone.utc)
    handle_sms("u", "applied stripe swe")  # this week
    store.create_application("u", "Ancient", "PM", applied_at=now - timedelta(days=60))
    reply = handle_sms("u", "what did I apply to this week")
    assert "Stripe" in reply
    assert "Ancient" not in reply


def test_recent_query_with_nothing_logged():
    reply = handle_sms("u", "what did I apply to today")
    assert "nothing logged" in reply.lower()
