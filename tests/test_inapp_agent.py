"""In-app agent: HELP_APP / SET_IDENTITY, POST /agent, heuristic-only chat router."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import applicant, config, engine, router
from app.engine import handle_sms
from app.intents import Intent
from app.main import app
from app.router import HeuristicRouter

R = HeuristicRouter()


def test_get_router_ignores_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    config.get_settings.cache_clear()
    router._router_singleton = None
    r = router.get_router()
    assert r.name == "heuristic"
    assert not isinstance(r, router.AnthropicRouter)


def test_help_app_autofill():
    p = R.parse("how do I autofill?")
    assert p.intent == Intent.HELP_APP
    reply = handle_sms("u_help", "how do I autofill?")
    assert "Autofill" in reply
    assert "Submit" in reply
    assert reply != engine.MENU


def test_help_app_how_this_works():
    p = R.parse("how does this work")
    assert p.intent == Intent.HELP_APP
    reply = handle_sms("u_help2", "how does this work")
    assert "horizon" in reply.lower() or "Autofill" in reply
    assert "LOG & UPDATE" not in reply


def test_navigate_settings():
    p = R.parse("take me to settings")
    assert p.intent == Intent.HELP_APP
    assert p.message == "tab:settings"
    reply = handle_sms("u_nav", "take me to settings")
    assert "Want me to take you to Settings" in reply
    assert "I'll take you there" not in reply


def test_open_apply_tab():
    p = R.parse("open Apply")
    assert p.intent == Intent.HELP_APP
    assert p.message == "tab:apply"


def test_set_identity_asks_for_value():
    p = R.parse("change my phone")
    assert p.intent == Intent.SET_IDENTITY
    reply = handle_sms("u_ask", "change my phone")
    assert "phone" in reply.lower()
    handle_sms("u_ask", "555-0199")
    assert applicant.get_identity("u_ask")["phone"] == "555-0199"


def test_set_identity_phone():
    p = R.parse("change my phone to 555-0100")
    assert p.intent == Intent.SET_IDENTITY
    assert p.role == "phone"
    assert "555-0100" in (p.message or "")
    reply = handle_sms("u_id", "change my phone to 555-0100")
    assert "phone" in reply.lower()
    assert applicant.get_identity("u_id")["phone"] == "555-0100"


def test_set_identity_live_in():
    p = R.parse("I live in Chicago, IL")
    assert p.intent == Intent.SET_IDENTITY
    assert p.role == "location"
    handle_sms("u_loc", "I live in Chicago, IL")
    assert "Chicago" in applicant.get_identity("u_loc")["location"]


def test_set_identity_sponsorship():
    handle_sms("u_sp", "I need sponsorship")
    assert applicant.get_identity("u_sp")["needs_sponsorship"] is True
    handle_sms("u_sp", "I don't need sponsorship")
    assert applicant.get_identity("u_sp")["needs_sponsorship"] is False


def test_profile_not_stolen_by_identity():
    p = R.parse("i'm looking for new grad swe roles, remote or nyc")
    assert p.intent == Intent.PROFILE


def test_edit_application_not_stolen_by_identity():
    p = R.parse("change the stripe role to SWE II")
    assert p.intent == Intent.EDIT


def test_how_do_i_apply_bare_is_help():
    assert R.parse("how do I apply").intent == Intent.HELP_APP
    assert R.parse("how do I apply to stripe").intent != Intent.HELP_APP


def test_help_cover_letter():
    p = R.parse("how do I attach a cover letter?")
    assert p.intent == Intent.HELP_APP
    reply = handle_sms("u_cover", "how do I attach a cover letter?")
    assert "cover letter" in reply.lower()
    assert "documents" in reply.lower()


def test_generic_help_is_horizon_overview():
    reply = handle_sms("u_menu", "help")
    assert "Horizon" in reply
    assert "LOG & UPDATE" not in reply


def test_commands_still_menu():
    assert handle_sms("u_menu", "commands") == engine.MENU
    assert handle_sms("u_menu", "menu") == engine.MENU


def _auth_client(monkeypatch) -> tuple[TestClient, dict]:
    monkeypatch.setenv("AUTH_ALLOW_DEV_LOGIN", "true")
    config.get_settings.cache_clear()
    c = TestClient(app)
    data = c.post("/auth/dev", json={"display_name": "Agent"}).json()
    headers = {"Authorization": f"Bearer {data['token']}"}
    return c, headers


def test_chat_returns_suggestions(monkeypatch):
    c, headers = _auth_client(monkeypatch)
    sent = c.post("/chat", headers=headers, json={"text": "how do I autofill?"})
    assert sent.status_code == 200
    body = sent.json()
    assert body["reply"]
    assert body["suggestions"] == ["Take me there", "Not now"]
    assert body["intent"] == "HELP_APP"
    assert not body.get("deep_link")
    assert "Autofill" in body["reply"]


def test_review_suggests_skip_queue_stop(monkeypatch):
    from app import jobstore
    from app.jobsources import JobPosting

    c, headers = _auth_client(monkeypatch)
    uid = c.get("/auth/me", headers=headers).json()["user"]["id"]
    jobstore.save_posting(
        uid,
        JobPosting("greenhouse", "1", "Role A", "https://x/1", company="Acme"),
        relevance_score=0.9, status="queued",
    )
    sent = c.post("/chat", headers=headers, json={"text": "review jobs"})
    assert sent.status_code == 200
    body = sent.json()
    assert "Reply:" not in body["reply"]
    assert body["suggestions"] == ["Skip", "Queue this", "Stop"]


def test_agent_set_identity(monkeypatch):
    c, headers = _auth_client(monkeypatch)
    sent = c.post("/agent", headers=headers, json={
        "action": "SET_IDENTITY",
        "raw_text": "change my email to a@b.com",
        "slots": {"field": "email", "value": "a@b.com"},
    })
    assert sent.status_code == 200
    body = sent.json()
    assert "email" in body["reply"].lower()
    assert "Open You" in (body.get("suggestions") or [])
    uid = body["user"]
    assert applicant.get_identity(uid)["email"] == "a@b.com"


def test_agent_queue_job_shape(monkeypatch):
    c, headers = _auth_client(monkeypatch)
    sent = c.post("/agent", headers=headers, json={
        "action": "HELP_APP",
        "raw_text": "take me to You",
        "slots": {"tab": "you"},
    })
    assert sent.status_code == 200
    body = sent.json()
    assert not body.get("deep_link")
    assert body["suggestions"] == ["Take me there", "Not now"]
    assert "Want me to take you to You" in body["reply"]


def test_hop_accept_emits_deep_link(monkeypatch):
    c, headers = _auth_client(monkeypatch)
    first = c.post("/chat", headers=headers, json={"text": "take me to settings"})
    assert not first.json().get("deep_link")
    yes = c.post("/chat", headers=headers, json={"text": "take me there"})
    assert yes.status_code == 200
    body = yes.json()
    assert body["deep_link"] == "settings"
    assert "heading to Settings" in body["reply"]


def test_hop_decline_stays(monkeypatch):
    c, headers = _auth_client(monkeypatch)
    c.post("/chat", headers=headers, json={"text": "open Apply"})
    no = c.post("/chat", headers=headers, json={"text": "not now"})
    assert no.status_code == 200
    body = no.json()
    assert not body.get("deep_link")
    assert "stay here" in body["reply"].lower()


def test_agent_unknown_falls_back_to_heuristic(monkeypatch):
    c, headers = _auth_client(monkeypatch)
    sent = c.post("/agent", headers=headers, json={
        "action": "UNKNOWN",
        "raw_text": "stats",
        "slots": {},
    })
    assert sent.status_code == 200
    assert sent.json()["reply"]


def test_chat_never_returns_empty_reply(monkeypatch):
    c, headers = _auth_client(monkeypatch)
    monkeypatch.setattr("app.chat.handle_sms", lambda *a, **k: "  ")
    sent = c.post("/chat", headers=headers, json={"text": "zzzz-unknown-xyz"})
    assert sent.status_code == 200
    body = sent.json()
    assert body["reply"].strip()
    assert body["assistant_message"]
    assert body["assistant_message"]["body"].strip()


def test_queue_unknown_does_not_deep_link(monkeypatch):
    c, headers = _auth_client(monkeypatch)
    sent = c.post("/chat", headers=headers, json={"text": "queue 999"})
    assert sent.status_code == 200
    body = sent.json()
    assert "don't have" in body["reply"].lower()
    assert not body.get("deep_link")


def test_queue_success_asks_before_moving(monkeypatch):
    from app import jobstore
    from app.jobsources import JobPosting

    c, headers = _auth_client(monkeypatch)
    uid = c.get("/auth/me", headers=headers).json()["user"]["id"]
    row = jobstore.save_posting(
        uid,
        JobPosting("greenhouse", "1", "Role A", "https://x/1", company="Acme"),
        relevance_score=0.9, status="queued",
    )
    sent = c.post("/chat", headers=headers, json={"text": f"queue {row['id']}"})
    assert sent.status_code == 200
    body = sent.json()
    assert not body.get("deep_link")
    assert body["suggestions"] == ["Take me there", "Not now"]
    assert "Want me to take you to that form on Apply" in body["reply"]


def test_review_does_not_deep_link(monkeypatch):
    from app import jobstore
    from app.jobsources import JobPosting

    c, headers = _auth_client(monkeypatch)
    uid = c.get("/auth/me", headers=headers).json()["user"]["id"]
    jobstore.save_posting(
        uid,
        JobPosting("greenhouse", "1", "Role A", "https://x/1", company="Acme"),
        relevance_score=0.9, status="queued",
    )
    sent = c.post("/chat", headers=headers, json={"text": "review jobs"})
    assert sent.status_code == 200
    assert not sent.json().get("deep_link")


def test_navigate_form_details_offers_hop_without_moving():
    p = R.parse("open form details")
    assert p.intent == Intent.HELP_APP
    assert p.message == "tab:you:identity"
    reply = handle_sms("u_ident_nav", "open form details")
    assert "Want me to take you to form details on You" in reply


def test_navigate_filed():
    p = R.parse("show me filed")
    assert p.intent == Intent.HELP_APP
    assert p.message == "tab:apply:filed"


def test_navigate_looking_for():
    p = R.parse("edit what I'm looking for")
    assert p.intent == Intent.HELP_APP
    assert p.message == "tab:you:search"


def test_navigate_add_fact_bare():
    p = R.parse("add a fact")
    assert p.intent == Intent.HELP_APP
    assert p.message == "tab:you:add"


def test_remember_not_stolen_by_add_fact():
    p = R.parse("remember project: built a compiler")
    assert p.intent == Intent.REMEMBER


def test_navigate_notifications():
    p = R.parse("turn on notifications")
    assert p.intent == Intent.HELP_APP
    assert p.message == "tab:settings:notifications"


def test_looking_for_roles_still_profile():
    p = R.parse("i'm looking for new grad swe roles, remote or nyc")
    assert p.intent == Intent.PROFILE


def test_hop_accept_form_details(monkeypatch):
    c, headers = _auth_client(monkeypatch)
    first = c.post("/chat", headers=headers, json={"text": "open form details"})
    assert not first.json().get("deep_link")
    yes = c.post("/chat", headers=headers, json={"text": "take me there"})
    assert yes.json()["deep_link"] == "you:identity"


def test_hop_accept_queued_job(monkeypatch):
    from app import jobstore
    from app.jobsources import JobPosting

    c, headers = _auth_client(monkeypatch)
    uid = c.get("/auth/me", headers=headers).json()["user"]["id"]
    row = jobstore.save_posting(
        uid,
        JobPosting("greenhouse", "1", "Role A", "https://x/1", company="Acme"),
        relevance_score=0.9, status="queued",
    )
    c.post("/chat", headers=headers, json={"text": f"queue {row['id']}"})
    yes = c.post("/chat", headers=headers, json={"text": "take me there"})
    assert yes.json()["deep_link"] == f"job:{row['id']}"


def test_knowledge_offers_you_hop(monkeypatch):
    c, headers = _auth_client(monkeypatch)
    sent = c.post("/chat", headers=headers, json={"text": "what's missing?"})
    body = sent.json()
    assert not body.get("deep_link")
    assert body["suggestions"] == ["Take me there", "Not now"]


def test_overview_lists_screens():
    reply = handle_sms("u_ov", "what can you do")
    assert "Apply" in reply
    assert "form details" in reply.lower() or "You" in reply
    assert not any(s in reply for s in ("Want me to take you",))


def test_agent_requires_auth():
    c = TestClient(app)
    assert c.post("/agent", json={"raw_text": "hi", "action": "UNKNOWN"}).status_code == 401


def test_health_chat_router_is_heuristic():
    c = TestClient(app)
    info = c.get("/health").json()
    assert info["chat_router"] == "heuristic"
    assert info["router"] == "heuristic"
