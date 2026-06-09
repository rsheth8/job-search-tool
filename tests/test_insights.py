"""Deck TL;DR insights: batched summarization, caching (summarize once), and the
fail-open / inactive paths. The real Haiku call is never made — tests inject a
summarizer, like every other LLM layer here."""
from __future__ import annotations

from app import insights


def _card(ext, title="Software Engineer", company="Acme", desc="Build things."):
    return {"source": "greenhouse", "external_id": ext, "title": title,
            "company": company, "description": desc}


def test_enrich_inactive_is_noop():
    cards = [_card("1")]
    out = insights.enrich(cards, "candidate")  # no key, no flag, no injected judge
    assert "tldr" not in out[0]


def test_enrich_fills_and_caches():
    calls = []

    def fake(cards, profile_block):
        calls.append(len(cards))
        return {i: {"tldr": f"does {c['title']}", "level": "Entry / new-grad",
                    "skills": "Python, SQL", "fit": "good entry fit"}
                for i, c in enumerate(cards)}

    cards = [_card("1"), _card("2", title="Data Analyst")]
    out = insights.enrich(cards, "candidate", summarize=fake)
    assert out[0]["tldr"] == "does Software Engineer" and out[0]["fit"] == "good entry fit"
    assert out[0]["level"] == "Entry / new-grad" and out[0]["skills"] == "Python, SQL"
    assert out[1]["tldr"] == "does Data Analyst"
    assert calls == [2]  # one batched call for both

    # Second time: served entirely from cache (all fields), summarizer NOT called.
    out2 = insights.enrich([_card("1"), _card("2", title="Data Analyst")],
                           "candidate", summarize=fake)
    assert out2[0]["tldr"] == "does Software Engineer" and out2[0]["level"] == "Entry / new-grad"
    assert calls == [2]  # unchanged — no new LLM call


def test_enrich_drops_not_stated_values():
    def fake(cards, profile_block):
        return {0: {"tldr": "builds things", "level": "Not stated",
                    "skills": "not stated", "fit": "ok fit"}}
    out = insights.enrich([_card("1")], "c", summarize=fake)
    assert out[0]["tldr"] == "builds things"
    assert "level" not in out[0] and "skills" not in out[0]  # 'Not stated' hidden


def test_enrich_only_summarizes_cache_misses():
    def fake(cards, profile_block):
        return {i: {"tldr": "t", "fit": "f"} for i, c in enumerate(cards)}

    insights.enrich([_card("1")], "c", summarize=fake)  # caches #1
    seen = {}

    def fake2(cards, profile_block):
        seen["n"] = len(cards)
        seen["ids"] = [c["external_id"] for c in cards]
        return {i: {"tldr": "t2", "fit": "f2"} for i, c in enumerate(cards)}

    out = insights.enrich([_card("1"), _card("2")], "c", summarize=fake2)
    assert seen["n"] == 1 and seen["ids"] == ["2"]  # only the miss
    assert out[0]["tldr"] == "t"   # #1 from cache
    assert out[1]["tldr"] == "t2"  # #2 freshly summarized


def test_enrich_fails_open_on_error():
    def boom(cards, profile_block):
        raise RuntimeError("model down")

    cards = [_card("1")]
    out = insights.enrich(cards, "c", summarize=boom)
    assert "tldr" not in out[0]  # no crash, no summary


def test_cache_helpers_roundtrip():
    insights._save_cached("greenhouse:9", {"tldr": "a tldr", "level": "Entry",
                                           "skills": "Python", "fit": "a fit"})
    got = insights._get_cached(["greenhouse:9", "missing:x"])
    assert got["greenhouse:9"] == {"tldr": "a tldr", "level": "Entry",
                                   "skills": "Python", "fit": "a fit"}
    assert "missing:x" not in got
