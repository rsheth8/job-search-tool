"""Discovery orchestration — fake sources + fake sender, no network/LLM."""
from __future__ import annotations

from app import discovery, jobstore, profile
from app.jobsources import JobPosting


class FakeSender:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, user_id, body):
        self.sent.append((user_id, body))


def _board_postings():
    return [
        JobPosting("greenhouse", "1", "Software Engineer", "https://x/1",
                   location="Remote", description="python kubernetes"),   # strong match
        JobPosting("greenhouse", "2", "Software Engineer", "https://x/2",
                   location="NYC", description="legacy maintenance"),       # weak match
        JobPosting("greenhouse", "3", "Sales Lead", "https://x/3",
                   location="NYC", description="quota"),                    # no match
    ]


def _setup(monkeypatch):
    jobstore.add_tracked_company("u", "greenhouse", "acme", "Acme")
    profile.set_profile("u", roles="software engineer", keywords="python, kubernetes")
    monkeypatch.setattr("app.discovery.fetch_source",
                        lambda source, token: _board_postings() if token == "acme" else [])


def test_tick_digest_mode_strong_match_only(monkeypatch):
    _setup(monkeypatch)
    sender = FakeSender()
    alerts = discovery.tick("u", sender=sender)

    assert alerts == 1
    assert len(sender.sent) == 1
    user, body = sender.sent[0]
    assert user == "u"
    assert "1 new job match" in body
    assert "Software Engineer" in body and "100%" in body
    assert "review jobs" in body

    statuses = {r["external_id"]: r["status"] for r in jobstore.list_postings("u")}
    assert statuses["greenhouse:acme:1"] == "queued"
    assert statuses["greenhouse:acme:2"] == "new"
    assert "greenhouse:acme:3" not in statuses


def test_tick_instant_mode_one_message_per_job(monkeypatch):
    _setup(monkeypatch)
    monkeypatch.setenv("JOB_ALERT_MODE", "instant")
    from app import config

    config.get_settings.cache_clear()
    sender = FakeSender()
    alerts = discovery.tick("u", sender=sender)
    assert alerts == 1
    assert len(sender.sent) == 1
    assert "#" in sender.sent[0][1]
    statuses = {r["external_id"]: r["status"] for r in jobstore.list_postings("u")}
    assert statuses["greenhouse:acme:1"] == "alerted"


def test_tick_dedupes_on_second_run(monkeypatch):
    _setup(monkeypatch)
    sender = FakeSender()
    assert discovery.tick("u", sender=sender) == 1
    # Nothing new the second time — every posting already recorded.
    assert discovery.tick("u", sender=sender) == 0
    assert len(sender.sent) == 1


def test_tick_no_boards_is_noop():
    assert discovery.tick("nobody", sender=FakeSender()) == 0


def test_run_all_sweeps_users_and_sets_last_tick(monkeypatch):
    _setup(monkeypatch)
    sender = FakeSender()
    total = discovery.run_all(sender=sender)
    assert total == 1
    assert discovery.last_tick_at is not None


def test_build_alert_body_includes_id_and_link():
    p = JobPosting("lever", "x", "Backend Engineer", "https://jobs/x",
                   company="Acme", location="Remote")
    body = discovery.build_alert_body(p, 0.83, 7)
    assert "Backend Engineer" in body
    assert "Acme" in body
    assert "https://jobs/x" in body
    assert "#7" in body and "83%" in body
