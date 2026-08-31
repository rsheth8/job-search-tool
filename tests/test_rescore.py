"""Re-scoring postings that are already stored.

Discovery scores a posting once and never again, so a scorer change or a profile
edit only moves rows found after it. This script is the escape hatch, and it
writes to the same table the feed reads — so what it will and won't touch is
worth pinning.
"""
from __future__ import annotations

from app import jobstore, profile
from app.jobsources import JobPosting
from scripts.rescore import rescore_user


def _seed(user="u", *, roles="software engineer, software intern",
          locations="chicago, remote"):
    profile.set_profile(user, roles=roles, locations=locations)
    posts = [
        JobPosting(source="greenhouse", external_id="1", company="Acme",
                   title="Software Engineering Intern, Summer 2027",
                   location="Chicago", url="https://x/1", description="join us"),
        JobPosting(source="greenhouse", external_id="2", company="Acme",
                   title="Software Engineer", location="Remote",
                   url="https://x/2", description="join us"),
        JobPosting(source="greenhouse", external_id="3", company="Acme",
                   title="Marketing Coordinator", location="Remote",
                   url="https://x/3", description="join us"),
    ]
    ids = [jobstore.save_posting(user, p, relevance_score=0.65)["id"] for p in posts]
    return user, ids


def _scores(user="u"):
    return {r["id"]: r["relevance_score"]
            for r in jobstore.list_postings(user, limit=500)}


def test_rescore_replaces_stale_scores():
    user, ids = _seed()
    before = _scores(user)
    assert set(before.values()) == {0.65}, "seeded flat on purpose"

    considered, changed = rescore_user(user, dry_run=False, min_change=0.001)
    assert considered == 3
    assert changed >= 2

    after = _scores(user)
    assert len(set(after.values())) > 1, "feed still flat after rescore"
    # The intern posting is a strong match for this profile; marketing is not.
    assert after[ids[0]] > after[ids[2]] + 0.5


def test_dry_run_writes_nothing():
    user, _ = _seed()
    before = _scores(user)
    considered, changed = rescore_user(user, dry_run=True, min_change=0.001)
    assert considered == 3
    assert changed >= 2
    assert _scores(user) == before


def test_min_change_skips_small_moves():
    user, _ = _seed()
    # Nothing moves a whole point, so a huge threshold means no writes.
    _, changed = rescore_user(user, dry_run=False, min_change=2.0)
    assert changed == 0
    assert set(_scores(user).values()) == {0.65}


def test_a_user_without_a_profile_is_skipped_not_neutralised():
    """Scoring with no profile returns a flat 0.5 for everything. Writing that
    over real scores would destroy the feed, so the script refuses instead."""
    jobstore.save_posting(
        "noprof",
        JobPosting(source="greenhouse", external_id="9", company="Acme",
                   title="Software Engineer", location="Remote",
                   url="https://x/9", description="join us"),
        relevance_score=0.8,
    )
    considered, changed = rescore_user("noprof", dry_run=False, min_change=0.001)
    assert (considered, changed) == (0, 0)
    assert list(_scores("noprof").values()) == [0.8]


def test_rescore_is_idempotent():
    user, _ = _seed()
    rescore_user(user, dry_run=False, min_change=0.001)
    settled = _scores(user)
    _, changed = rescore_user(user, dry_run=False, min_change=0.001)
    assert changed == 0
    assert _scores(user) == settled


def test_rescore_leaves_other_users_alone():
    _seed("a")
    _seed("b")
    before_b = _scores("b")
    rescore_user("a", dry_run=False, min_change=0.001)
    assert _scores("b") == before_b


def test_rescore_does_not_touch_status_or_ordering_columns():
    user, ids = _seed()
    jobstore.mark_posting_status(ids[0], "dismissed")
    rescore_user(user, dry_run=False, min_change=0.001)
    rows = {r["id"]: r for r in jobstore.list_postings(user, limit=500)}
    assert rows[ids[0]]["status"] == "dismissed"


def test_rescore_never_calls_the_paid_scorer(monkeypatch):
    """It must be safe (and free) to run against production."""
    called = []
    monkeypatch.setattr("app.matcher._llm_score",
                        lambda *a, **k: called.append(1) or {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-looks-real")
    from app import config
    config.get_settings.cache_clear()

    user, _ = _seed()
    rescore_user(user, dry_run=False, min_change=0.001)
    assert called == []


def test_empty_user_is_a_noop():
    assert rescore_user("nobody", dry_run=False, min_change=0.001) == (0, 0)
