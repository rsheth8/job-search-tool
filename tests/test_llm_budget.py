"""Per-user daily LLM cap fail-opens to the heuristic router."""
from __future__ import annotations

from app import config, llm_budget, router


def test_consume_unlimited_when_cap_zero(monkeypatch):
    monkeypatch.setenv("LLM_MAX_CALLS_PER_USER_PER_DAY", "0")
    config.get_settings.cache_clear()
    llm_budget.set_user("u1")
    assert llm_budget.consume() is True
    assert llm_budget.consume() is True


def test_consume_stops_at_cap(monkeypatch):
    monkeypatch.setenv("LLM_MAX_CALLS_PER_USER_PER_DAY", "2")
    config.get_settings.cache_clear()
    llm_budget.set_user("u_cap")
    assert llm_budget.consume() is True
    assert llm_budget.consume() is True
    assert llm_budget.consume() is False
    assert llm_budget.calls_today("u_cap") == 2
    # A different user still has budget.
    llm_budget.set_user("u_other")
    assert llm_budget.consume() is True


def test_router_falls_back_when_capped(monkeypatch):
    monkeypatch.setenv("LLM_MAX_CALLS_PER_USER_PER_DAY", "1")
    config.get_settings.cache_clear()
    llm_budget.set_user("u_router")
    llm_budget.consume()  # spend the only slot

    class FakeAnthropic(router.AnthropicRouter):
        def __init__(self):  # noqa: D107 — skip live client
            self._fallback = router.HeuristicRouter()
            self._limiter = type("L", (), {"allow": staticmethod(lambda: True)})()
            self._max_chars = 480
            self._system = []
            self._model = "fake"
            self.usage = {"calls": 0, "fallbacks": 0, "input_tokens": 0,
                          "output_tokens": 0, "cache_read_tokens": 0,
                          "cache_write_tokens": 0}

        def parse_actions(self, text: str):
            return super().parse_actions(text)

    fake = FakeAnthropic()
    # Cap is spent; parse_actions should not call the network (no _client).
    actions = fake.parse_actions("applied stripe swe")
    assert actions
    assert fake.usage["fallbacks"] >= 1
