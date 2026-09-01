"""Chips that refill.

The complaint was that the quiz offers eight skills and then stops, so anyone
whose stack isn't in that eight has to type. The fix is a large catalog plus a
ranking: after each tap the client asks for the next batch, and what comes back
is related to the tap rather than being the next entry in a static list.

So these tests care about two things: that the pool never runs dry, and that
what surfaces is actually adjacent to what was picked. A test that only checked
"twelve strings came back" would pass on a shuffled list.
"""
from __future__ import annotations

import pytest

from app import applicant, profile, suggest


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _names(payload) -> list[str]:
    return [s.lower() for s in payload["suggestions"]]


# --- the ranking ----------------------------------------------------------

def test_picking_react_surfaces_the_rest_of_the_frontend():
    out = suggest.next_batch("skills", ["React"], limit=8)
    assert "next.js" in _names(out)
    assert "typescript" in _names(out)


def test_picking_pytorch_surfaces_ml_not_the_head_of_the_catalog():
    """Python leads the catalog. Under a pick it must lose to the neighbours."""
    plain = suggest.next_batch("skills", [], limit=6)["suggestions"]
    assert plain[:3] == ["Python", "JavaScript", "TypeScript"], "catalog order moved"

    out = _names(suggest.next_batch("skills", ["PyTorch"], limit=6))
    assert "scikit-learn" in out
    assert "tensorflow" in out or "keras" in out


def test_two_picks_in_the_same_cluster_beat_one():
    """Weights add up, so a consistent story pulls harder than a single tap."""
    one = suggest.next_batch("skills", ["Docker"], limit=30)["suggestions"]
    two = suggest.next_batch("skills", ["Docker", "Kubernetes"], limit=30)["suggestions"]
    assert two.index("Terraform") <= one.index("Terraform")


def test_a_pick_is_never_offered_back():
    out = suggest.next_batch("skills", ["Python", "React", "AWS"], limit=40)
    assert not {"python", "react", "aws"} & set(_names(out))


def test_matching_is_case_and_punctuation_insensitive():
    """The stored value is whatever the user typed; the catalog is canonical."""
    out = _names(suggest.next_batch("skills", ["node.js", "REST apis"], limit=40))
    assert "node.js" not in out
    assert "rest apis" not in out


def test_a_neighbouring_city_comes_before_a_far_one():
    out = suggest.next_batch("locations", ["Minneapolis"], limit=6)["suggestions"]
    assert "St. Paul" in out
    assert "Tokyo" not in out


def test_context_seeds_the_very_first_batch():
    """Before any tap, what we already know about them should still rank."""
    cold = _names(suggest.next_batch("skills", [], limit=10))
    warm = _names(suggest.next_batch("skills", [], context=["ML engineer"], limit=10))
    assert "pytorch" not in cold
    assert "pytorch" in warm


def test_a_pick_outweighs_context():
    """Their tap is evidence; their stored profile is a guess."""
    out = suggest.next_batch(
        "skills", ["SwiftUI"], context=["Data scientist", "Data Science"], limit=6
    )["suggestions"]
    assert "iOS" in out or "UIKit" in out


# --- the pool doesn't run dry ---------------------------------------------

def test_the_catalog_outlasts_a_determined_tapper():
    """Tap fifty skills; there is always another batch."""
    chosen: list[str] = []
    for _ in range(50):
        batch = suggest.next_batch("skills", chosen, limit=4)
        assert batch["suggestions"], f"ran dry after {len(chosen)} picks"
        assert batch["remaining"] > 0
        chosen.append(batch["suggestions"][0])
    assert len(set(s.lower() for s in chosen)) == 50, "repeated a suggestion"


def test_remaining_counts_what_is_left_not_what_was_sent():
    out = suggest.next_batch("skills", [], limit=10)
    assert out["remaining"] == len(suggest.CATALOG["skills"]) - 10


def test_every_field_has_a_catalog_worth_refilling():
    for field in ("skills", "roles", "locations", "disciplines"):
        assert len(suggest.CATALOG[field]) >= 40, field


def test_no_catalog_lists_the_same_term_twice():
    for field, terms in suggest.CATALOG.items():
        seen = [suggest._norm(t) for t in terms]
        assert len(seen) == len(set(seen)), f"{field} has duplicates"


def test_every_clustered_term_exists_in_some_catalog():
    """A typo in the cluster map is silent: it just never matches anything."""
    known = {suggest._norm(t) for terms in suggest.CATALOG.values() for t in terms}
    unknown = sorted(
        term for members in suggest._CLUSTERS.values()
        for term in members if suggest._norm(term) not in known
    )
    assert unknown == [], f"clustered but not in any catalog: {unknown}"


def test_an_unknown_field_is_empty_not_an_explosion():
    out = suggest.next_batch("favourite_colour", [])
    assert out == {"field": "favourite_colour", "suggestions": [],
                   "remaining": 0, "known": False}


def test_a_query_filters_for_typeahead():
    out = suggest.next_batch("skills", [], query="script", limit=20)["suggestions"]
    assert "JavaScript" in out and "TypeScript" in out
    assert "Docker" not in out


def test_limit_is_clamped_both_ways():
    assert len(suggest.next_batch("skills", [], limit=0)["suggestions"]) == suggest.DEFAULT_LIMIT
    assert len(suggest.next_batch("skills", [], limit=9999)["suggestions"]) == suggest.MAX_LIMIT


def test_a_huge_chosen_list_is_bounded():
    """The chosen list arrives in a query string; don't let it drive the cost."""
    out = suggest.next_batch("skills", [f"junk{i}" for i in range(5000)], limit=5)
    assert len(out["suggestions"]) == 5


# --- the endpoint ---------------------------------------------------------

def test_endpoint_returns_a_batch(client):
    body = client.get("/apply/suggestions?user=u1&field=skills&limit=5").json()
    assert body["user"] == "u1"
    assert body["field"] == "skills"
    assert len(body["suggestions"]) == 5
    assert body["remaining"] > 0


def test_endpoint_excludes_what_the_client_already_has(client):
    body = client.get(
        "/apply/suggestions?user=u1&field=skills&chosen=Python,React&limit=20"
    ).json()
    assert not {"python", "react"} & set(_names(body))
    assert "next.js" in _names(body)


def test_endpoint_ranks_against_the_stored_profile(client):
    """The reason this is a server endpoint and not a bundled JSON file."""
    profile.set_profile("u1", roles="ML engineer")
    body = client.get("/apply/suggestions?user=u1&field=skills&limit=12").json()
    assert "pytorch" in _names(body)

    body = client.get("/apply/suggestions?user=u2&field=skills&limit=12").json()
    assert "pytorch" not in _names(body), "u2's batch leaked u1's profile"


def test_endpoint_uses_the_degree_list_for_context(client):
    """A second degree in a different field should count toward the ranking."""
    applicant.set_identity("u1", {"education": [
        {"school": "State", "degree": "B.A.", "discipline": "English"},
        {"school": "State", "degree": "M.S.", "discipline": "Cybersecurity"},
    ]})
    body = client.get("/apply/suggestions?user=u1&field=skills&limit=12").json()
    assert "penetration testing" in _names(body) or "cryptography" in _names(body)


def test_endpoint_survives_a_user_with_no_profile_at_all(client):
    body = client.get("/apply/suggestions?user=nobody&field=roles&limit=6").json()
    assert len(body["suggestions"]) == 6


def test_endpoint_handles_a_plus_in_a_pick(client):
    """"C++" has to arrive as "C++".

    A bare "+" in a query string decodes to a space, which made picking C++
    exclude "C" and leave "C++" on offer -- the one chip that could never be
    dismissed. The client percent-encodes it; this is the server half.
    """
    body = client.get(
        "/apply/suggestions?user=u1&field=skills&chosen=C%2B%2B,C%23&limit=40"
    ).json()
    assert "C++" not in body["suggestions"]
    assert "C#" not in body["suggestions"]
    assert "C" in body["suggestions"], "excluded a different language"


def test_endpoint_field_is_case_insensitive(client):
    body = client.get("/apply/suggestions?user=u1&field=Skills&limit=3").json()
    assert body["known"] is True


def test_endpoint_is_token_gated(client, monkeypatch):
    monkeypatch.setenv("APPLY_API_TOKEN", "secret")
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        assert client.get("/apply/suggestions?user=u1&field=skills").status_code == 401
        ok = client.get("/apply/suggestions?user=u1&field=skills",
                        headers={"X-Apply-Token": "secret"})
        assert ok.status_code == 200
    finally:
        get_settings.cache_clear()
