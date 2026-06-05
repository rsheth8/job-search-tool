"""Reputability filter for discovered postings."""
from __future__ import annotations

from app.jobsources import JobPosting
from app.jobsources import quality


def _p(source, title="Software Engineer", company="Acme", url="https://x/1"):
    return JobPosting(source=source, external_id=title, title=title, url=url,
                      company=company)


def test_first_party_sources_always_kept():
    # Even a thin posting from a real ATS is trusted (first-party career system).
    for src in ("greenhouse", "lever", "ashby"):
        assert quality.is_reputable(_p(src, company="", url=""))


def test_generic_company_dropped_from_aggregator():
    assert not quality.is_reputable(_p("aggregator", company="Top Company"))
    assert not quality.is_reputable(_p("aggregator", company="Confidential"))
    assert not quality.is_reputable(_p("rss", company=""))


def test_aggregator_needs_apply_url():
    assert not quality.is_reputable(_p("aggregator", company="Stripe", url=""))
    assert quality.is_reputable(_p("aggregator", company="Stripe", url="https://job/1"))


def test_spam_title_dropped():
    assert not quality.is_reputable(
        _p("aggregator", title="Earn $5000/week from home today!", company="WFH Inc")
    )


def test_filter_reputable_counts():
    posts = [
        _p("greenhouse", company="Acme"),          # kept (first-party)
        _p("aggregator", company="Top Company"),    # dropped (placeholder)
        _p("aggregator", company="Stripe"),         # kept (real + url)
        _p("rss", company="", url=""),              # dropped (no company/url)
    ]
    kept, dropped = quality.filter_reputable(posts)
    assert {p.company for p in kept} == {"Acme", "Stripe"}
    assert dropped == 2
