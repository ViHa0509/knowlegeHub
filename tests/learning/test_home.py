"""/home admin page and its routes (§4.10)."""
from __future__ import annotations

import base64
import hashlib
import re

import pytest

from app import distill, home
from tests.auth_helpers import ISSUER, bearer, enable_signin, make_token
from tests.learning.conftest import item


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._d = status, data

    def json(self):
        return self._d


class _KC:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def post(self, url, data=None, timeout=None):  # noqa: ARG002
        self.calls.append((url, dict(data or {})))
        return self.answers.pop(0)


def test_page_and_config(client):
    r = client.get("/home")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.headers["content-type"].startswith("text/html") and "<html" in r.text.lower()
    c = client.get("/home/config").json()
    assert c == {"auth": False, "client_id": "kh-cli", "base": None, "kh_version": c["kh_version"],
                 "emb_model": "kh-hash-384"}


def test_login_proxy(client, monkeypatch):
    assert client.post("/home/login/start").status_code == 409
    enable_signin(monkeypatch)
    kc = _KC([_Resp(200, {"device_code": "dc", "user_code": "UC", "verification_uri": "https://kc/v",
                          "verification_uri_complete": "https://kc/v?c=UC", "expires_in": 600, "interval": 5}),
              _Resp(400, {"error": "authorization_pending"}),
              _Resp(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 300}),
              _Resp(400, {"error": "invalid_grant"})])
    monkeypatch.setattr(home, "HTTP", kc)
    s = client.post("/home/login/start").json()
    assert set(s) == {"device_code", "user_code", "verification_uri", "verification_uri_complete", "expires_in",
                      "interval", "code_verifier"}
    url, data = kc.calls[0]
    assert url == f"{ISSUER}/protocol/openid-connect/auth/device" and data["code_challenge_method"] == "S256"
    want = base64.urlsafe_b64encode(hashlib.sha256(s["code_verifier"].encode()).digest()).decode().rstrip("=")
    assert data["code_challenge"] == want
    body = {"device_code": "dc", "code_verifier": s["code_verifier"]}
    assert client.post("/home/login/poll", json=body).json() == {"state": "pending"}
    ok = client.post("/home/login/poll", json=body).json()
    assert ok == {"state": "ok", "access_token": "at", "refresh_token": "rt", "expires_in": 300}
    assert kc.calls[2][1]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert kc.calls[2][1]["code_verifier"] == s["code_verifier"]
    f = client.post("/home/login/refresh", json={"refresh_token": "rt"}).json()
    assert f["state"] == "failed" and kc.calls[3][1]["grant_type"] == "refresh_token"


def test_projects_items_delete(client, uid, add_log, add_know):
    from app import projects

    p = f"hm-{uid}"
    projects.get_or_create_project(p)
    ids = [add_know(p, f"knowledge row {i} " + "z" * (700 if i == 0 else 10), ts=f"2026-10-0{i + 1}T00:00:00Z",
                    kind="lesson") for i in range(3)]
    add_log(p, "a log row")
    pr = {x["id"]: x for x in client.get("/home/projects").json()["projects"]}[p]
    assert set(pr["parts"]) == {"knowledge", "log", "findings", "entity", "chunk", "policy", "src"}
    assert pr["parts"]["knowledge"] == 3 and pr["parts"]["log"] == 1 and "graph" in pr
    j = client.get("/home/items", params={"project": p, "part": "knowledge", "limit": 2}).json()
    assert j["total"] == 3 and j["sorted"] is True
    assert [x["id"] for x in j["items"]] == [ids[2], ids[1]]                     # newest first
    it = client.get("/home/items", params={"project": p, "part": "knowledge", "offset": 2}).json()["items"][0]
    assert it["id"] == ids[0] and len(it["text"]) == 600 and it["length"] > 600 and it["part"] == "knowledge"
    assert set(it) == {"id", "time", "text", "length", "meta", "part"}
    assert set(it["meta"]) == {"kind", "topic", "subject", "status", "author", "title", "scope", "category",
                               "action", "context"} and it["meta"]["kind"] == "lesson"
    assert len(client.get("/home/items", params={"project": p, "part": "knowledge", "limit": 0}).json()["items"]) == 3
    assert client.get("/home/items", params={"project": p, "part": "bogus"}).status_code == 422
    d = client.post("/home/items/delete", json={"project": p, "part": "knowledge", "ids": [ids[0]]}).json()
    assert d["deleted"] == 1
    assert client.get("/home/items", params={"project": p, "part": "knowledge"}).json()["total"] == 2
    r = client.post("/home/parts/delete", json={"project": p, "part": "knowledge", "confirm": "nope"})
    assert r.status_code == 422
    r = client.post("/home/parts/delete", json={"project": p, "part": "knowledge", "confirm": p}).json()
    assert r["deleted"] == 2
    assert client.post("/home/parts/delete", json={"project": "orchestrator", "part": "graph",
                                                   "confirm": "orchestrator"}).status_code == 403
    assert client.post("/home/parts/delete", json={"project": p, "part": "graph", "confirm": p}).status_code == 200


def test_members_and_personal(client, monkeypatch):
    assert client.get("/home/members", params={"project": "x"}).status_code == 409   # sign-in off
    enable_signin(monkeypatch)
    from app import auth

    calls = []
    monkeypatch.setattr(auth, "relay", lambda m, path, body=None, timeout=10.0: (calls.append((m, path, body)) or
                                                                                (200, {"ok": True})))
    h = bearer(make_token("boss@x.com", role="maintainer"))
    assert client.get("/home/members", params={"project": "px"}, headers=h).json() == {"ok": True}
    assert client.put("/home/members", json={"project": "px", "email": "a@x.com", "level": "editable"},
                      headers=h).status_code == 200
    assert client.put("/home/members", json={"project": "px", "email": "a@x.com", "level": "god"},
                      headers=h).status_code == 422
    client.post("/home/members/remove", json={"project": "px", "email": "a@x.com"}, headers=h)
    assert [c[0] for c in calls] == ["GET", "PUT", "DELETE"]
    assert calls[1][2] == {"email": "a@x.com", "level": "editable"}
    m = bearer(make_token("mem@x.com"))
    assert client.get("/home/projects", headers=m).status_code == 403
    assert client.get("/home/config", headers=m).status_code == 200
    assert client.get("/home").status_code == 200
    e = client.post("/home/personal/ensure", json={"emails": ["New.Person@X.com", "bad"]}, headers=h).json()
    assert e["ensured"] == [{"email": "new.person@x.com", "project": "personal-new-person-x-com"}]


def test_lessons(client, proj, add_log, distil):
    r = client.post("/home/lessons", json={"project": proj, "title": "Manual lesson", "text": "Do the thing."})
    assert r.status_code == 200
    lid = r.json()["id"]
    assert re.fullmatch(r"dk_[0-9a-f]{12}", lid)
    it = distill.get_item(proj, lid)
    m = it["metadata"]
    assert it["text"] == "Manual lesson: Do the thing." and m["status"] == "active" and m["knowledge_type"] == "quick"
    assert m["distilled_by"] == "manual:local" and m["action"] == "" and m["context"] == ""
    assert client.post("/home/lessons", json={"project": proj, "title": "", "text": "x"}).status_code == 422
    a = add_log(proj, "we decided a proposed lesson")
    _nj, s = distil(proj, lambda nj: [item([a], "Proposed one", "Proposed body."),
                                      item([a], "Second one", "Another, totally different body about pasta.")])
    pid1, pid2 = [x["id"] for x in s.json()["results"]]
    ls = client.get("/home/lessons", params={"project": proj}).json()
    assert ls["count"] == 3 and {x["id"] for x in ls["lessons"]} == {lid, pid1, pid2}
    assert set(ls["lessons"][0]) == {"id", "title", "text", "status", "category", "time", "by"}
    ap = client.post("/home/lessons/decide", json={"project": proj, "ids": [pid1], "decision": "approve"}).json()
    assert ap["results"][0]["result"] == "active" and ap["shared"] == []
    rj = client.post("/home/lessons/decide", json={"project": proj, "ids": [pid2], "decision": "reject",
                                                   "reason": "dup"}).json()
    assert rj["results"][0]["result"] == "withdrawn"
    ids = {x["id"] for x in client.get("/home/lessons", params={"project": proj}).json()["lessons"]}
    assert ids == {lid, pid1}
    assert client.post("/home/lessons/decide", json={"project": proj, "ids": [], "decision": "maybe"}).status_code == 422


@pytest.mark.parametrize("path", ["/home/projects", "/home/lessons?project=x", "/home/items?project=x"])
def test_maintainer_only(client, monkeypatch, path):
    enable_signin(monkeypatch)
    assert client.get(path, headers=bearer(make_token("m@x.com", projects={"x": "owner"}))).status_code == 403
