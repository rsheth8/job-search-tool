"""Phase 3 ghost-job filter: conservative content rules + repost signal."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import jobstore
from app.jobsources import ghost
from app.jobsources.base import JobPosting


_REAL_DESC = (
    "We are hiring a software engineer to build and maintain our backend "
    "services. You will collaborate with a small team across the stack, own "
    "features end to end, and ship to production regularly."
)


def _p(title="Software Engineer", desc=_REAL_DESC,
       source="aggregator", company="Acme", posted_at="", ext="1") -> JobPosting:
    return JobPosting(source=source, external_id=ext, title=title,
                      url="https://acme.com/jobs/1", company=company,
                      location="Remote", description=desc, posted_at=posted_at)


# --- high-precision single signals drop on their own ---

def test_evergreen_language_is_ghost():
    assert ghost.is_ghost(_p(desc="We are always accepting applications for our talent pool."))


def test_personal_email_contact_is_ghost():
    assert ghost.is_ghost(_p(desc="Send your resume to recruiter99@gmail.com to apply today."))


def test_comp_hype_alone_is_not_quite_ghost_but_with_corroboration_is():
    # 0.5 alone stays under threshold (conservative)...
    hype = ("Great opportunity! Earn $5000 per week working from anywhere. "
            "We provide full training and support for motivated individuals.")
    assert not ghost.is_ghost(_p(desc=hype))
    # ...but combined with a thin/stale corroborator it trips.
    assert ghost.is_ghost(_p(desc="Earn $$$ now!", posted_at="50 days ago"))


def test_reposted_many_times_is_ghost():
    assert ghost.is_ghost(_p(), repost_count=3)
    # A single repost is not enough on its own.
    assert not ghost.is_ghost(_p(), repost_count=2)


def test_repost_plus_stale_combines_to_ghost():
    assert ghost.is_ghost(_p(posted_at="30+ days ago"), repost_count=2)


# --- conservative: real postings survive ---

def test_normal_posting_is_not_ghost():
    assert not ghost.is_ghost(_p(desc="We're hiring a backend engineer to build our payments platform. "
                                       "You'll work with Go and Postgres on a small team."))


def test_stale_alone_is_not_ghost():
    # Staleness is corroborating evidence, never decisive by itself.
    assert not ghost.is_ghost(_p(posted_at="90 days ago"))


def test_thin_description_alone_is_not_ghost():
    assert not ghost.is_ghost(_p(desc="Apply now."))


def test_first_party_is_always_trusted():
    # Even with blatant ghost text, a first-party ATS posting is never dropped.
    p = _p(desc="Always accepting applications for our talent pool, email me@gmail.com",
           source="greenhouse")
    assert ghost.ghost_signals(p) == []
    assert not ghost.is_ghost(p, repost_count=9)


def test_is_stale_parses_iso_and_human_strings():
    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
    assert ghost._is_stale(old)
    assert ghost._is_stale("45 days ago")
    assert ghost._is_stale("30+ days ago")
    assert not ghost._is_stale("2 days ago")
    assert not ghost._is_stale("")
    assert not ghost._is_stale("garbage")


# --- repost counting against the store ---

def test_seen_similar_count_uses_role_synonyms():
    jobstore.save_posting("u1", _p(title="Software Engineer", ext="a"))
    jobstore.save_posting("u1", _p(title="Software Engineer II", ext="b"))
    jobstore.save_posting("u1", _p(title="Sales Director", ext="c"))
    # "SWE" should match the two software-engineer rows, not sales.
    assert jobstore.seen_similar_count("u1", "Acme", "SWE") == 2
    # Different company doesn't count.
    assert jobstore.seen_similar_count("u1", "Globex", "Software Engineer") == 0
    assert jobstore.seen_similar_count("u1", "", "SWE") == 0
