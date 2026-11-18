"""POST /messages/send, GET /messages/sent, POST /findings/resolve, GET /personalization/guard_tools (KH 7.x)."""
from __future__ import annotations

from app import projects


def test_send_and_sent(client, fresh_store):
    projects.get_or_create_project("beta")
    assert client.post("/messages/send", json={"content": " ", "to": "a@example.com"}).json()["detail"] == \
        "content is empty"
    assert client.post("/messages/send", json={"content": "hi"}).status_code == 400
    d = client.post("/messages/send", json={"content": "private note", "to": "Ann@Example.com", "private": True}).json()
    assert d["channel"] == "direct" and d["to"] == "ann@example.com"
    p = client.post("/messages/send", json={"content": "look at this", "project": "beta",
                                            "mentions": ["bob@example.com"]}).json()
    assert p["channel"] == "project" and p["mentions"] == ["bob@example.com"]
    plain = client.post("/messages/send", json={"content": "no binding so private", "to": "cy@example.com"}).json()
    assert plain["channel"] == "direct"
    s = client.get("/messages/sent").json()
    ids = [m["id"] for m in s["messages"]]
    assert d["id"] in ids and p["id"] in ids
    one = client.get("/messages/sent", params={"id": d["id"]}).json()["messages"][0]
    assert one["recipients"] == [{"to": "ann@example.com", "delivered": False, "delivered_at": None,
                                  "opened": False, "opened_at": None}]
    assert client.get("/messages/sent", params={"id": "nope"}).status_code == 404
    assert client.post("/messages/send", json={"content": "x", "reply_to": "nope"}).status_code == 404
    r = client.post("/messages/send", json={"content": "answer", "reply_to": p["id"]}).json()
    assert r["channel"] == "project" and r["reply_to"] == p["id"]


def test_findings_resolve(client, fresh_store):
    projects.get_or_create_project("beta")
    fid = client.post("/vector/add", json={"text": "Login page times out on QA", "project": "beta",
                                           "scope": "findings"}).json()["vector_id"]
    assert client.post("/findings/resolve", json={}).json()["detail"] == "project and id required"
    assert client.post("/findings/resolve", json={"project": "beta", "id": fid}).json()["detail"].startswith(
        "solution required")
    assert client.post("/findings/resolve", json={"project": "beta", "id": "nope", "solution": "x"}).json() == \
        {"detail": "no finding 'nope' in project 'beta'"}
    r = client.post("/findings/resolve", json={"project": "beta", "id": fid,
                                               "solution": "Raise the QA gateway timeout to 60s"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["finding"] == fid and j["lesson"] == f"beta/lesson-learns/{j['lesson_id']}"
    sc = {s["scope"]: s["rows"] for s in client.get("/spaces/beta/scopes").json()["scopes"]}
    assert sc["findings"] == 0 and sc["lesson-learns"] == 1
    row = client.get(f"/spaces/beta/rows/{j['lesson']}").json()
    assert row["type"] == "lesson" and "SOLUTION: Raise the QA gateway timeout to 60s" in row["text"]


def test_guard_tools(client):
    j = client.get("/personalization/guard_tools").json()
    assert isinstance(j["tools"], list)
