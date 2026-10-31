"""Feedback counter and findings (§4.7)."""
from __future__ import annotations

from app import feedback, projects
from app import vector_store as vs


def test_issue_tick_counter(client, uid):
    p = f"fbk-{uid}"
    t = client.post("/feedback/tick", json={"project": p, "n": 3}).json()
    assert t == {"project": p, "request_total": 3}
    r = client.post("/feedback/issue", json={"project": p, "issue_id": "wrong-port", "desc": "used port 80"})
    assert r.status_code == 200
    j = r.json()
    assert j["issue_id"] == "wrong-port" and j["count"] == 1 and j["opened_at_request"] == 3
    assert j["desc"] == "used port 80" and j["last_marked_count"] == 0
    j = client.post("/feedback/issue", json={"project": p, "issue_id": "wrong-port"}).json()
    assert j["count"] == 2 and j["desc"] == "used port 80"
    c = client.get("/feedback/counter", params={"project": p}).json()
    assert c["request_total"] == 3
    assert set(c["issues"]["wrong-port"]) == {"issue_id", "desc", "count", "opened_at_request", "last_marked_count",
                                              "created_at", "updated_at"}
    assert c["issues"]["wrong-port"]["count"] == 2
    assert client.post("/feedback/issue", json={"project": p, "issue_id": " "}).status_code == 422
    long = client.post("/feedback/issue", json={"project": p, "issue_id": "long", "desc": "d" * 3000}).json()
    assert len(long["desc"]) == 2000


def test_sweep_marks_once_and_resolve(client, uid):
    p = f"fbs-{uid}"
    for _ in range(3):
        client.post("/feedback/issue", json={"project": p, "issue_id": "flaky", "desc": "the flaky login test"})
    assert p not in client.post("/feedback/sweep").json()["marked"]          # count 3 is not > 3
    assert client.get("/findings/list", params={"project": p}).json()["findings"] == []
    client.post("/feedback/issue", json={"project": p, "issue_id": "flaky"})
    sw = client.post("/feedback/sweep").json()
    assert sw["marked"][p] == 1
    assert p not in client.post("/feedback/sweep").json()["marked"]          # once per count
    lst = client.get("/findings/list", params={"project": p}).json()
    assert lst["count"] == 1
    f = lst["findings"][0]
    assert f["id"] == "finding__flaky" and f["issue_id"] == "flaky" and f["count"] == 4 and f["status"] == "open"
    assert f["desc"] == "the flaky login test"
    h = vs.get_existing(vs.part_collection_name("findings", p))
    m = vs.get_ids_vt(h, ["finding__flaky"])[0]["metadata"]
    assert m["kind"] == "finding" and m["project"] == p and m["ts"]
    assert client.get("/feedback/counter", params={"project": p}).json()["issues"]["flaky"]["last_marked_count"] == 4
    # a higher count re-marks
    client.post("/feedback/issue", json={"project": p, "issue_id": "flaky"})
    assert client.post("/feedback/sweep").json()["marked"][p] == 1
    # query by meaning
    q = client.post("/findings/query", json={"project": p, "intent": "the flaky login test"}).json()
    assert q["findings"][0]["id"] == "finding__flaky" and q["findings"][0]["score"] >= 0.35
    q = client.post("/findings/query", json={"project": p, "query": "zebra pasta", "threshold": 0.99}).json()
    assert q["findings"] == []
    assert client.post("/findings/query", json={"project": p}).status_code == 422
    # resolve clears both
    r = client.post("/feedback/resolve", json={"project": p, "issue_id": "flaky"}).json()
    assert r == {"issue_id": "flaky", "resolved": True, "issue_cleared": True, "finding_cleared": True}
    assert client.get("/findings/list", params={"project": p}).json()["count"] == 0
    r = client.post("/feedback/reset", json={"project": p, "issue_id": "flaky"}).json()
    assert r == {"issue_id": "flaky", "resolved": False, "issue_cleared": False, "finding_cleared": False}


def test_legacy_counter_imported_once(client, uid):
    p = f"fbl-{uid}"
    projects.write_counter(p, {"request_total": 7, "issues": {"old": {"desc": "legacy", "count": 2}}})
    c = client.get("/feedback/counter", params={"project": p}).json()
    assert c["request_total"] == 7 and c["issues"]["old"]["count"] == 2
    client.get("/feedback/counter", params={"project": p})
    assert client.get("/feedback/counter", params={"project": p}).json()["request_total"] == 7


def test_defaults():
    assert feedback.min_count() == 3


def test_auth_levels(client, uid, monkeypatch):
    from tests.auth_helpers import bearer, enable_signin, make_token

    enable_signin(monkeypatch)
    p = f"fba-{uid}"
    ro = bearer(make_token("ro@x.com", projects={p: "readonly"}))
    ed = bearer(make_token("ed@x.com", projects={p: "editable"}))
    assert client.post("/feedback/issue", json={"project": p, "issue_id": "a"}, headers=ro).status_code == 403
    assert client.post("/feedback/tick", json={"project": p, "n": 1}, headers=ro).status_code == 403
    assert client.post("/feedback/issue", json={"project": p, "issue_id": "a"}, headers=ed).status_code == 200
    assert client.get("/feedback/counter", params={"project": p}, headers=ro).status_code == 200
    assert client.get("/findings/list", params={"project": p}, headers=ro).status_code == 200
    assert client.post("/feedback/sweep", headers=ed).status_code == 403
