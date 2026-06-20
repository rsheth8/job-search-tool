"""Swipe trainer: deck building/dedup, label recording, stats, the /train/*
endpoints, and that swipe labels actually train the re-ranker."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import reranker, trainer
from app.jobsources import JobPosting
from app.main import app


def _posting(title, ext, source="greenhouse", company="Acme", desc="Build software."):
    return JobPosting(source=source, external_id=ext, title=title, url="https://x",
                      company=company, location="Remote", description=desc)


def _deck_fetch(postings):
    return lambda: list(postings)


# ---------------------------------------------------------------------------
# Deck
# ---------------------------------------------------------------------------

def test_build_deck_returns_scored_cards():
    fetch = _deck_fetch([_posting("Software Engineer", "1"), _posting("Sales Rep", "2")])
    deck = trainer.build_deck("u1", fetch=fetch)
    assert len(deck) == 2
    card = deck[0]
    assert {"source", "external_id", "company", "title", "relevance_score"} <= card.keys()
    # Sorted best-match first (a score is always present).
    assert deck[0]["relevance_score"] >= deck[1]["relevance_score"]


def test_diverse_mode_skips_prefilter_to_surface_off_target():
    # One posting matches the profile; the rest don't. Normal mode prefilters down
    # to the match; Mix mode keeps the wider pool so there are roles to reject.
    posts = [
        _posting("ML Engineer", "0", company="Co0", desc="machine learning systems"),
        _posting("Frontend Engineer", "1", company="Co1", desc="react ui work"),
        _posting("DevOps Engineer", "2", company="Co2", desc="kubernetes and ci"),
        _posting("Security Engineer", "3", company="Co3", desc="appsec and audits"),
        _posting("Mobile Engineer", "4", company="Co4", desc="ios and android"),
    ]
    from app import profile
    profile.set_profile("u1", roles="machine learning")
    normal = trainer.build_deck("u1", limit=6, fetch=_deck_fetch(posts))
    mixed = trainer.build_deck("u1", limit=6, diverse=True, fetch=_deck_fetch(posts))
    assert {c["title"] for c in normal} == {"ML Engineer"}   # prefiltered to the match
    assert len(mixed) > len(normal)                          # Mix is wider
    assert "Frontend Engineer" in {c["title"] for c in mixed}  # off-target surfaces


def test_uncertain_mode_surfaces_least_confident_first():
    """Active learning: with a trained model, the deck is ordered by how uncertain
    the model is about each posting (predicted probability nearest 0.5 first),
    which differs from the plain relevance sort."""
    from app import jobstore, matcher, profile

    profile.set_profile("u1", roles="software engineer", locations="remote")
    # Train a model that keys on title_hit + first_party (apply = SWE roles on a
    # company ATS; pass = non-SWE roles off an aggregator), so postings land at a
    # spread of confidences rather than all-or-nothing.
    for i in range(8):
        jobstore.save_posting("u1", _posting("Software Engineer", f"p{i}",
                              company=f"Co{i}", source="greenhouse"),
                              relevance_score=0.6, status="applied")
    for i in range(8):
        jobstore.save_posting("u1", _posting("Data Entry Clerk", f"n{i}",
                              company=f"X{i}", source="rss", desc="filing"),
                              relevance_score=0.2, status="dismissed")
    prof = profile.get_profile("u1")
    assert reranker.train("u1", prof) is not None

    # Distinct (company, title) so none are deduped; mixed signals create a spread.
    posts = [
        _posting("Software Engineer", "a", company="Alpha", source="greenhouse"),
        _posting("Data Entry Clerk", "b", company="Bravo", source="rss", desc="filing"),
        _posting("Software Engineer", "c", company="Charlie", source="rss"),
    ]
    deck = trainer.build_deck("u1", limit=3, uncertain=True, fetch=_deck_fetch(posts))

    # Expected order computed the same way the deck does (heuristic relevance →
    # model probability → sort by closeness to 0.5).
    scored = matcher.score(posts, prof, allow_llm=False, allow_embeddings=False)
    preds = reranker.predict("u1", prof, scored)
    expected = [p.external_id for p, _ in sorted(preds, key=lambda t: abs(t[1] - 0.5))]
    assert [c["external_id"] for c in deck] == expected
    # And it's genuinely an active-learning reorder, not the relevance sort.
    relevance_order = [p.external_id for p, _ in
                       sorted(scored, key=lambda t: t[1], reverse=True)]
    assert expected != relevance_order


def test_uncertain_mode_falls_back_to_relevance_without_model():
    # Cold start (no trained model): Learn mode is safe — it just returns a deck
    # rather than erroring, ordered by the heuristic relevance.
    posts = [_posting("Software Engineer", "1"), _posting("Sales Rep", "2")]
    deck = trainer.build_deck("u1", uncertain=True, fetch=_deck_fetch(posts))
    assert len(deck) == 2
    assert deck[0]["relevance_score"] >= deck[1]["relevance_score"]


def test_build_deck_excludes_already_labeled():
    p = _posting("Software Engineer", "1")
    trainer.record_label("u1", trainer._card(p, 0.5), "like")
    deck = trainer.build_deck("u1", fetch=_deck_fetch([p, _posting("Other", "2")]))
    ids = {c["external_id"] for c in deck}
    assert "1" not in ids and "2" in ids


def test_build_deck_dedupes_within_batch():
    dup = _posting("Software Engineer", "1")
    deck = trainer.build_deck("u1", fetch=_deck_fetch([dup, dup]))
    assert len(deck) == 1


def test_build_deck_survives_fetch_error():
    def boom():
        raise RuntimeError("board down")
    assert trainer.build_deck("u1", fetch=boom) == []


# ---------------------------------------------------------------------------
# Labels + stats
# ---------------------------------------------------------------------------

def test_record_label_is_idempotent():
    card = trainer._card(_posting("Software Engineer", "1"), 0.7)
    assert trainer.record_label("u1", card, "like") is True
    assert trainer.record_label("u1", card, "pass") is False  # already labeled


def test_stats_counts_and_thresholds():
    for i in range(3):
        trainer.record_label("u1", trainer._card(_posting("Eng", f"L{i}"), 0.8), "like")
    for i in range(2):
        trainer.record_label("u1", trainer._card(_posting("Sales", f"P{i}"), 0.2), "pass")
    st = trainer.stats("u1")
    assert st["likes"] == 3 and st["passes"] == 2
    assert st["need_likes"] == 2 and st["need_passes"] == 3  # min 5 each
    assert st["model_trained"] is False


# ---------------------------------------------------------------------------
# Swipes train the re-ranker
# ---------------------------------------------------------------------------

def test_swipe_labels_train_reranker():
    # 5 likes on greenhouse roles, 5 passes on rss roles.
    for i in range(5):
        trainer.record_label("u1", trainer._card(
            _posting("Engineer", f"gh{i}", source="greenhouse"), 0.6), "like")
    for i in range(5):
        trainer.record_label("u1", trainer._card(
            _posting("Engineer", f"rss{i}", source="rss"), 0.6), "pass")
    model = reranker.train("u1", None)
    assert model is not None
    assert model["n_labels"] == 10


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

def test_train_page_serves_html():
    client = TestClient(app)
    r = client.get("/train?user=u1")
    assert r.status_code == 200
    assert "Train your matcher" in r.text


def test_deck_and_label_endpoints(monkeypatch):
    # Force the deck source so the endpoint never hits the network (directory + RSS).
    from app import trainer as t
    monkeypatch.setattr(
        t, "_default_sources", lambda: [_posting("Software Engineer", "1")],
    )
    client = TestClient(app)
    deck = client.get("/train/deck?user=u1&n=5").json()
    assert deck["user"] == "u1"
    assert len(deck["cards"]) == 1

    card = deck["cards"][0]
    st = client.post("/train/label", json={"user": "u1", "label": "like", "item": card}).json()
    assert st["likes"] == 1
    # Same card again is ignored (dedupe).
    st2 = client.post("/train/label", json={"user": "u1", "label": "pass", "item": card}).json()
    assert st2["likes"] == 1 and st2["passes"] == 0
