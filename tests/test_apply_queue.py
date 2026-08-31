"""Semi-auto application queue (Track C): staging, package assembly (offline
template path), status transitions, and that nothing auto-submits."""
from __future__ import annotations

from app import apply_queue, jobstore
from app.jobsources import JobPosting


def _posting(ext="1", title="Software Engineer", company="Acme"):
    return JobPosting(source="greenhouse", external_id=ext, title=title,
                      url="https://x/apply", company=company, location="Remote",
                      description="Build software.")


def _save(user="u1", **kw) -> int:
    row = jobstore.save_posting(user, _posting(**kw), relevance_score=0.7,
                                status="queued")
    return row["id"]


def test_stage_is_idempotent_and_validates_ownership():
    pid = _save()
    assert apply_queue.stage("u1", pid) is True
    assert apply_queue.stage("u1", pid) is False        # already staged
    assert apply_queue.stage("u1", 99999) is False      # no such posting
    assert apply_queue.stage("other", pid) is False     # not this user's posting


def test_list_queue_joins_posting_fields():
    pid = _save(title="ML Engineer", company="Beta")
    apply_queue.stage("u1", pid)
    items = apply_queue.list_queue("u1")
    assert len(items) == 1
    it = items[0]
    assert it["posting_id"] == pid and it["title"] == "ML Engineer"
    assert it["company"] == "Beta" and it["status"] == "staged"
    assert it["has_answers"] is False and it["has_resume"] is False


def test_get_package_assembles_tailored_questions():
    pid = _save()
    apply_queue.stage("u1", pid)
    pkg = apply_queue.get_package("u1", pid)
    assert pkg is not None
    assert pkg["url"] == "https://x/apply"
    assert pkg["resume"] is None               # tailoring disabled in tests
    qs = pkg["questions"]
    assert len(qs) == len(apply_queue.COMMON_QUESTIONS)
    assert all(q["question"] and q["answer"] for q in qs)   # one answer each
    assert "Acme" in qs[0]["question"]         # company interpolated
    # Cached now, persisted on the row.
    assert apply_queue.list_queue("u1")[0]["has_answers"] is True
    # Re-opening returns the same cached questions (no re-draft).
    assert apply_queue.get_package("u1", pid)["questions"] == qs


def test_get_package_includes_identity():
    from app import applicant
    applicant.set_identity("u1", {"first_name": "Ada", "email": "ada@x.com",
                                  "city": "Chicago", "state": "IL"})
    pid = _save()
    apply_queue.stage("u1", pid)
    pkg = apply_queue.get_package("u1", pid)
    assert pkg["identity"]["email"] == "ada@x.com"
    assert pkg["identity"]["location"] == "Chicago, IL"   # derived


def test_save_answer_persists_one_question():
    pid = _save()
    apply_queue.stage("u1", pid)
    apply_queue.get_package("u1", pid)                     # assemble first
    assert apply_queue.save_answer("u1", pid, 1, "My edited answer.") is True
    qs = apply_queue.get_package("u1", pid)["questions"]
    assert qs[1]["answer"] == "My edited answer."
    assert qs[0]["answer"] != "My edited answer."          # only #1 changed
    assert apply_queue.save_answer("u1", pid, 99, "x") is False   # bad index
    assert apply_queue.save_answer("u1", 99999, 0, "x") is False  # not staged


def test_redraft_regenerates_one_question():
    pid = _save()
    apply_queue.stage("u1", pid)
    apply_queue.save_answer("u1", pid, 0, "stale")
    fresh = apply_queue.redraft_answer("u1", pid, 0)
    assert fresh and fresh != "stale"
    assert apply_queue.get_package("u1", pid)["questions"][0]["answer"] == fresh
    assert apply_queue.redraft_answer("u1", pid, 99) is None      # bad index
    assert apply_queue.redraft_answer("u1", 99999, 0) is None     # not staged


def test_get_package_missing_item_returns_none():
    pid = _save()
    # Posting exists but was never staged.
    assert apply_queue.get_package("u1", pid) is None


def test_mark_transitions_and_validates_status():
    pid = _save()
    apply_queue.stage("u1", pid)
    assert apply_queue.mark("u1", pid, "ready") is True
    assert apply_queue.list_queue("u1", status="ready")[0]["posting_id"] == pid
    assert apply_queue.mark("u1", pid, "submitted") is True
    assert apply_queue.mark("u1", pid, "bogus") is False     # invalid status
    assert apply_queue.mark("u1", 99999, "ready") is False   # missing item


def test_remove_drops_item():
    pid = _save()
    apply_queue.stage("u1", pid)
    assert apply_queue.remove("u1", pid) is True
    assert apply_queue.list_queue("u1") == []
    assert apply_queue.remove("u1", pid) is False


def test_promote_moves_item_to_front():
    first = _save(ext="1")
    apply_queue.stage("u1", first)
    second = _save(ext="2")
    apply_queue.stage("u1", second)
    ids = [i["posting_id"] for i in apply_queue.list_queue("u1")]
    assert ids[0] == second
    assert apply_queue.promote("u1", first) is True
    ids = [i["posting_id"] for i in apply_queue.list_queue("u1")]
    assert ids[0] == first


def test_promote_stages_if_needed():
    pid = _save()
    assert apply_queue.promote("u1", pid) is True
    assert apply_queue.list_queue("u1")[0]["posting_id"] == pid
    assert apply_queue.promote("u1", 99999) is False
    assert apply_queue.promote("other", pid) is False


def test_reorder_ready_queue():
    a = _save(ext="1")
    apply_queue.stage("u1", a)
    b = _save(ext="2")
    apply_queue.stage("u1", b)
    # Newest staged is first until the user ranks them.
    assert [i["posting_id"] for i in apply_queue.list_queue("u1")][0] == b
    assert apply_queue.reorder("u1", [a, b]) is True
    assert [i["posting_id"] for i in apply_queue.list_queue("u1")] == [a, b]
    assert apply_queue.reorder("u1", [99999]) is False
    assert apply_queue.reorder("u1", []) is True


# ---------------------------------------------------------------------------
# Web endpoints
# ---------------------------------------------------------------------------

def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_endpoints_stage_review_and_mark():
    pid = _save()
    c = _client()
    # Top matches list the un-staged queued posting.
    data = c.get("/apply/data?user=u1").json()
    assert any(q["posting_id"] == pid for q in data["queued"])

    assert c.post("/apply/stage", json={"user": "u1", "posting_id": pid}).json()["ok"]
    data = c.get("/apply/data?user=u1").json()
    assert [q["posting_id"] for q in data["queued"]] == []   # no longer offered
    assert data["queue"][0]["posting_id"] == pid             # now in the queue

    pkg = c.post("/apply/package", json={"user": "u1", "posting_id": pid}).json()
    assert pkg["questions"] and pkg["url"] == "https://x/apply"

    assert c.post("/apply/mark", json={"user": "u1", "posting_id": pid,
                                       "status": "submitted"}).json()["ok"]
    # A submitted item leaves the queue -- it belongs on the Filed pane. This
    # used to assert queue[0]["status"] == "submitted", which encoded the bug:
    # the phone reads queue.first as "up next", so the job you had just applied
    # to stayed at the top of the dashboard and nothing advanced.
    after = c.get("/apply/data?user=u1").json()
    assert [q["posting_id"] for q in after["queue"]] == []
    assert [q["posting_id"] for q in after["queued"]] == []  # nor re-offered


def test_applied_logs_application_and_marks_posting():
    """The mobile 'I applied' button records the application and marks the posting."""
    from app import store
    pid = _save(company="Acme")
    c = _client()
    assert c.post("/apply/applied", json={"user": "u1", "posting_id": pid}).json()["ok"]
    assert any(a["company"] == "Acme" for a in store.list_applications("u1"))
    assert jobstore.get_posting("u1", pid)["status"] == "applied"


def test_answer_save_and_redraft_endpoints():
    pid = _save()
    c = _client()
    c.post("/apply/stage", json={"user": "u1", "posting_id": pid})
    c.post("/apply/package", json={"user": "u1", "posting_id": pid})

    assert c.post("/apply/answer/save",
                  json={"user": "u1", "posting_id": pid, "index": 0,
                        "answer": "edited"}).json()["ok"]
    pkg = c.post("/apply/package", json={"user": "u1", "posting_id": pid}).json()
    assert pkg["questions"][0]["answer"] == "edited"

    fresh = c.post("/apply/answer/redraft",
                   json={"user": "u1", "posting_id": pid, "index": 0}).json()["answer"]
    assert fresh and fresh != "edited"


def test_resume_endpoint_404_when_unavailable():
    pid = _save()
    apply_queue.stage("u1", pid)
    # Resume tailoring is disabled in tests -> no PDF to serve.
    assert _client().get(f"/apply/resume?user=u1&id={pid}").status_code == 404


def test_cover_endpoint_404_when_unavailable():
    pid = _save()
    apply_queue.stage("u1", pid)
    assert _client().get(f"/apply/cover?user=u1&id={pid}").status_code == 404


def test_cover_endpoint_serves_pdf(monkeypatch):
    pid = _save()
    apply_queue.stage("u1", pid)
    monkeypatch.setattr(
        apply_queue, "build_cover_bytes",
        lambda uid, posting_id: (b"%PDF-cover", "Cover_Letter_Backend_Acme.pdf"),
    )
    r = _client().get(f"/apply/cover?user=u1&id={pid}")
    assert r.status_code == 200
    assert r.content == b"%PDF-cover"
    assert r.headers["content-type"].startswith("application/pdf")
    assert "Cover_Letter_Backend_Acme.pdf" in r.headers["content-disposition"]
