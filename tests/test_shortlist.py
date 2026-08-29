"""Apply-today ranking: fillable + fresh beats a high-score aggregator link."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import shortlist
from app.jobsources import JobPosting


def _p(**kw) -> JobPosting:
    base = dict(
        source="rss", external_id="1", title="Software Engineer",
        url="https://remoteok.com/jobs/1", company="Acme",
        location="Remote", description="Build backend services.",
        posted_at="",
    )
    base.update(kw)
    return JobPosting(**base)


def test_fillable_fresh_greenhouse_outranks_higher_score_rss():
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    gh = _p(
        source="greenhouse", external_id="g",
        url="https://boards.greenhouse.io/acme/jobs/1",
        posted_at=(now - timedelta(hours=12)).isoformat(),
    )
    rss = _p(
        source="rss", external_id="r",
        url="https://remoteok.com/jobs/99",
        posted_at=(now - timedelta(days=20)).isoformat(),
    )
    ranked = shortlist.rank_scored([(rss, 0.99, 1), (gh, 0.61, 2)], now=now)
    assert ranked[0][0].source == "greenhouse"
    assert ranked[1][0].source == "rss"


def test_freshness_score_decays():
    now = datetime(2026, 8, 29, tzinfo=timezone.utc)
    assert shortlist.freshness_score(
        (now - timedelta(hours=6)).isoformat(), now=now
    ) == 1.0
    assert shortlist.freshness_score(
        (now - timedelta(days=60)).isoformat(), now=now
    ) == 0.0
    mid = shortlist.freshness_score(
        (now - timedelta(days=20)).isoformat(), now=now
    )
    assert 0.0 < mid < 1.0
    assert shortlist.freshness_score("") == 0.3


def test_user_pin_wins_over_auto_rank():
    class Row(dict):
        def __getitem__(self, key):
            return dict.get(self, key)

    pinned = Row(
        url="https://remoteok.com/x", source="rss", posted_at="",
        relevance_score=0.4, sort_order=0,
    )
    auto = Row(
        url="https://boards.greenhouse.io/acme/jobs/1", source="greenhouse",
        posted_at="", relevance_score=0.9, sort_order=None,
    )
    out = shortlist.rank_rows([auto, pinned])
    assert out[0] is pinned
    assert out[1] is auto
