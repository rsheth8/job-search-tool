"""Phase 1 embedding scoring: vector math, serialization, gating, and the
matcher integration (with the real Voyage backend never touched — tests inject a
fake embedder, exactly like the LLM scorer is faked elsewhere)."""
from __future__ import annotations

import math
import sqlite3

import pytest

from app import embeddings, jobstore, matcher
from app.config import get_settings
from app.jobsources import JobPosting


# ---------------------------------------------------------------------------
# Vector helpers
# ---------------------------------------------------------------------------

def test_cosine_identical_orthogonal_and_scale_invariant():
    assert embeddings.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert embeddings.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    # Magnitude doesn't matter, only direction.
    assert embeddings.cosine([1.0, 1.0], [3.0, 3.0]) == pytest.approx(1.0)


def test_cosine_handles_missing_and_degenerate():
    assert embeddings.cosine(None, [1.0]) == 0.0
    assert embeddings.cosine([1.0], None) == 0.0
    assert embeddings.cosine([1.0, 2.0], [1.0]) == 0.0  # length mismatch
    assert embeddings.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # zero vector


def test_blob_roundtrip_and_empty():
    vec = [0.5, -1.25, 3.0, 0.0]
    blob = embeddings.to_blob(vec)
    assert isinstance(blob, bytes)
    out = embeddings.from_blob(blob)
    assert out == pytest.approx(vec)
    assert embeddings.to_blob(None) is None
    assert embeddings.to_blob([]) is None
    assert embeddings.from_blob(None) is None
    assert embeddings.from_blob(b"") is None


# ---------------------------------------------------------------------------
# embed() degradation — never raises, returns aligned Nones
# ---------------------------------------------------------------------------

def test_embed_inactive_returns_nones():
    # conftest leaves embeddings disabled / unkeyed.
    assert get_settings().embedding_active is False
    assert embeddings.embed(["a", "b"]) == [None, None]
    assert embeddings.embed([]) == []


def test_embed_degrades_on_api_error(monkeypatch):
    monkeypatch.setenv("EMBEDDING_ENABLED", "true")
    monkeypatch.setenv("VOYAGE_API_KEY", "vk-test")
    get_settings.cache_clear()
    embeddings.reset_for_tests()
    assert get_settings().embedding_active is True

    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "post", boom)
    # Never raises; aligned Nones so the caller falls back to the heuristic.
    assert embeddings.embed(["x", "y", "z"]) == [None, None, None]
    # A failed call is NOT counted against the daily budget.
    assert jobstore.embedding_calls_today() == 0


def test_embed_daily_cap_blocks(monkeypatch):
    monkeypatch.setenv("EMBEDDING_ENABLED", "true")
    monkeypatch.setenv("VOYAGE_API_KEY", "vk-test")
    monkeypatch.setenv("EMBEDDING_MAX_CALLS_PER_DAY", "0")
    get_settings.cache_clear()
    embeddings.reset_for_tests()
    # Over budget → no network attempt, just Nones.
    assert embeddings.embed(["x"]) == [None]


def test_embed_parses_voyage_response(monkeypatch):
    monkeypatch.setenv("EMBEDDING_ENABLED", "true")
    monkeypatch.setenv("VOYAGE_API_KEY", "vk-test")
    get_settings.cache_clear()
    embeddings.reset_for_tests()

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            # Deliberately out of order to prove index-mapping.
            return {"data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]}

    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: FakeResp())
    out = embeddings.embed(["first", "second"])
    assert out == [[1.0, 0.0], [0.0, 1.0]]
    assert jobstore.embedding_calls_today() == 1  # success counted


# ---------------------------------------------------------------------------
# matcher.score with an injected fake embedder
# ---------------------------------------------------------------------------

def _profile(roles="machine learning engineer", **extra) -> sqlite3.Row:
    cols = {"roles": roles, "keywords": "", "locations": "", "seniority": "",
            "resume_summary": "", "min_relevance": None}
    cols.update(extra)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    keys = ", ".join(cols)
    qs = ", ".join("?" * len(cols))
    conn.execute(f"CREATE TABLE p ({keys})")
    conn.execute(f"INSERT INTO p ({keys}) VALUES ({qs})", tuple(cols.values()))
    return conn.execute("SELECT * FROM p").fetchone()


def _posting(title, desc="", ext="1") -> JobPosting:
    return JobPosting(source="greenhouse", external_id=ext, title=title,
                      url="https://x", company="Acme", location="Remote",
                      description=desc)


def test_score_uses_injected_embedder_and_sets_vector():
    prof = _profile("machine learning")
    near = _posting("ML Engineer", ext="1")
    far = _posting("Sales Rep", ext="2")

    # Fake: query vector points one way; the "ML" doc aligns with it, sales doesn't.
    def fake_embed(texts, *, input_type="document"):
        if input_type == "query":
            return [[1.0, 0.0]]
        return [[1.0, 0.0] if "ML" in t else [0.0, 1.0] for t in texts]

    scored = matcher.score([near, far], prof, embedder=fake_embed)
    by_title = {p.title: s for p, s in scored}
    assert by_title["ML Engineer"] > by_title["Sales Rep"]
    # Vector stashed on the posting for persistence (embed once).
    assert near.embedding == [1.0, 0.0]


def test_score_per_posting_embed_miss_falls_back_to_heuristic():
    prof = _profile("machine learning")
    p = _posting("ML Engineer", desc="machine learning role", ext="1")

    def fake_embed(texts, *, input_type="document"):
        if input_type == "query":
            return [[1.0, 0.0]]
        return [None for _ in texts]  # doc embed missing

    scored = matcher.score([p], prof, embedder=fake_embed)
    assert len(scored) == 1
    assert p.embedding is None  # nothing to stash
    assert 0.0 <= scored[0][1] <= 1.0


def test_score_empty_profile_falls_through_to_heuristic():
    prof = _profile(roles="")  # nothing to compare against

    def fake_embed(texts, *, input_type="document"):
        raise AssertionError("should not embed with an empty profile")

    scored = matcher.score([_posting("Anything")], prof, embedder=fake_embed)
    assert scored[0][1] == pytest.approx(0.5)  # neutral heuristic


def test_save_posting_persists_embedding():
    p = _posting("ML Engineer", ext="emb1")
    p.embedding = [0.1, 0.2, 0.3]
    row = jobstore.save_posting("u1", p, relevance_score=0.9, status="queued")
    assert row is not None
    stored = jobstore.get_posting("u1", row["id"])
    assert embeddings.from_blob(stored["embedding"]) == pytest.approx([0.1, 0.2, 0.3])
