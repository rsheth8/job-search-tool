"""LLM browser-agent logic (worker/agent.py) — exercised with fakes so no real
browser or Anthropic call is made. The browser-driving itself still needs a live
test, but the action routing, EEO guard, and loop bookkeeping are covered here."""
from __future__ import annotations

import json
import types

from worker import agent


# --- fakes -----------------------------------------------------------------

class FakeFrame:
    def __init__(self, elements):
        self._elements = elements
        self.calls = []

    def evaluate(self, _js, prefix):
        # Re-tag with the given frame prefix, mirroring the real snapshot JS.
        return [{**e, "aid": prefix + str(i)} for i, e in enumerate(self._elements)]

    def fill(self, sel, text): self.calls.append(("fill", sel, text))
    def select_option(self, sel, label=None): self.calls.append(("select", sel, label))
    def check(self, sel): self.calls.append(("check", sel))
    def click(self, sel): self.calls.append(("click", sel))
    def set_input_files(self, sel, files=None): self.calls.append(("upload", sel))


class FakeMouse:
    def __init__(self): self.wheels = []
    def wheel(self, x, y): self.wheels.append((x, y))


class FakePage:
    def __init__(self, frame):
        self._frame = frame
        self.frames = [frame]
        self.url = "https://boards.greenhouse.io/acme/jobs/1"
        self.mouse = FakeMouse()
        self.goto_url = None

    def goto(self, url, **kw): self.goto_url = url
    def wait_for_timeout(self, _ms): pass
    def screenshot(self, **kw): return b"\xff\xd8jpeg"


def _block(name, inp, bid="tu1"):
    return types.SimpleNamespace(type="tool_use", name=name, input=inp, id=bid)


class FakeClient:
    """Yields a scripted sequence of tool calls, one per .create() call."""
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

    class _Messages:
        def __init__(self, outer): self.outer = outer
        def create(self, **kw):
            self.outer.calls.append(kw)
            block = self.outer._scripted.pop(0)
            return types.SimpleNamespace(content=[block])

    @property
    def messages(self): return FakeClient._Messages(self)


# --- act() routing ---------------------------------------------------------

def test_act_fill_targets_right_frame_and_selector():
    fr = FakeFrame([])
    page = FakePage(fr)
    res, done, out = agent.act(page, [fr], "fill", {"aid": "f0_3", "text": "Ada"},
                               None, lambda a: "First name")
    assert (res, done, out) == ("filled", False, None)
    assert fr.calls == [("fill", '[data-agent-id="f0_3"]', "Ada")]


def test_act_eeo_field_is_never_filled():
    fr = FakeFrame([])
    res, done, _ = agent.act(FakePage(fr), [fr], "fill",
                             {"aid": "f0_1", "text": "x"}, None,
                             lambda a: "Gender identity")
    assert "EEO" in res and not fr.calls   # guard fired; nothing typed


def test_act_upload_resume_without_resume_is_noop():
    fr = FakeFrame([])
    res, _, _ = agent.act(FakePage(fr), [fr], "upload_resume", {"aid": "f0_0"},
                          None, lambda a: "Resume")
    assert "no resume" in res and not fr.calls


def test_act_ready_and_blocked_are_terminal():
    fr = FakeFrame([])
    _, done, out = agent.act(FakePage(fr), [fr], "ready_for_review",
                             {"summary": "done"}, None, lambda a: "")
    assert done and out["status"] == "ready"
    _, done2, out2 = agent.act(FakePage(fr), [fr], "blocked",
                               {"reason": "captcha"}, None, lambda a: "")
    assert done2 and out2 == {"status": "blocked", "reason": "captcha"}


# --- run_agent loop --------------------------------------------------------

def test_identity_field_is_autofilled_by_code_not_the_llm():
    fr = FakeFrame([{"kind": "text", "label": "First name"}])
    page = FakePage(fr)
    # The LLM is only consulted for the handoff — the name is filled deterministically.
    client = FakeClient([_block("ready_for_review", {"summary": "done"}, "b")])
    job = {"url": page.url, "identity": {"first_name": "Ada"}, "questions": []}
    preview = agent.run_agent(page, job, client)

    assert preview["status"] == "ready"
    assert preview["filled"] == [{"label": "First name", "value": "Ada"}]
    assert fr.calls == [("fill", '[data-agent-id="f0_0"]', "Ada")]  # filled by code
    assert len(client.calls) == 1                  # exactly one LLM call (efficiency)
    assert preview["screenshot_url"].startswith("data:image/jpeg")
    assert page.goto_url == job["url"]
    # The profile is cached on the system prompt (prompt caching).
    assert client.calls[0]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_auto_fill_handles_text_select_and_resume_without_llm():
    fr = FakeFrame([])
    page = FakePage(fr)
    elements = [
        {"aid": "f0_0", "kind": "text", "label": "Email", "value": ""},
        {"aid": "f0_1", "kind": "select", "label": "Are you authorized to work?",
         "options": ["Select…", "Yes", "No"], "value": "Select…"},
        {"aid": "f0_2", "kind": "file", "label": "Resume / CV"},
        {"aid": "f0_3", "kind": "text", "label": "Why do you want this job?", "value": ""},
        {"aid": "f0_4", "kind": "text", "label": "Email", "value": "a@x.com"},  # already set
    ]
    identity = {"email": "a@x.com", "work_authorized": "Yes"}
    resume = {"name": "resume.pdf", "mimeType": "application/pdf", "buffer": b"x"}
    acted = agent.auto_fill(page, [fr], elements, identity, resume, set())

    assert {"label": "Email", "value": "a@x.com"} in acted
    assert {"label": "Are you authorized to work?", "value": "Yes"} in acted
    assert {"label": "Resume / CV", "value": "resume.pdf"} in acted
    # The essay and the already-filled email are left alone (LLM / nothing to do).
    assert not any(a["label"] == "Why do you want this job?" for a in acted)
    assert ("fill", '[data-agent-id="f0_4"]', "a@x.com") not in fr.calls


def test_auto_fill_attempted_set_prevents_refilling():
    fr = FakeFrame([])
    elements = [{"aid": "f0_0", "kind": "text", "label": "First name", "value": ""}]
    attempted: set = set()
    first = agent.auto_fill(FakePage(fr), [fr], elements, {"first_name": "Ada"},
                            None, attempted)
    second = agent.auto_fill(FakePage(fr), [fr], elements, {"first_name": "Ada"},
                             None, attempted)
    assert first and not second        # second pass is a no-op — no infinite loop


def test_run_agent_blocked_is_surfaced():
    fr = FakeFrame([{"kind": "text", "label": "Email"}])
    page = FakePage(fr)
    client = FakeClient([_block("blocked", {"reason": "login wall"}, "a")])
    preview = agent.run_agent(page, {"url": page.url, "identity": {}, "questions": []},
                              client)
    assert preview["status"] == "blocked" and preview["reason"] == "login wall"


def test_profile_text_includes_identity_and_answers():
    txt = agent._profile_text(
        {"email": "a@x.com", "phone": ""},
        [{"question": "Why us?", "answer": "I love it"}])
    assert "email: a@x.com" in txt
    assert "phone" not in txt            # empty values dropped
    assert "Why us?" in txt and "I love it" in txt
