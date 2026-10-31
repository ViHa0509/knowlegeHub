"""§2.8 project endpoints, §2.10/§3.10 project match, /orchestrator/resolve|shortcut|fact|switch."""
from __future__ import annotations

import pytest

from app import auth, project_access, projects
from tests.auth_helpers import bearer, make_token


@pytest.fixture
def kc(monkeypatch):
    """Fake identity service: records relays; `kc.answer[(method, path)] = (status, body)`."""
    class KC:
        calls: list = []
        answer: dict = {}

    k = KC()
    k.calls, k.answer = [], {}

    def relay(method, path, body=None, timeout=10.0):
        k.calls.append((method, path, body, auth.current_token() is not None))
        return k.answer.get((method, path), (200, {}))

    monkeypatch.setattr(auth, "relay", relay)
    return k


def maint(uid):
    return bearer(make_token(f"boss{uid}@x.com", role="maintainer"))


def test_create_project_flow(signed_client, kc, uid):
    pid = f"proj{uid}"
    creator = bearer(make_token(f"c{uid}@x.com", permissions=["project.create"]))
    nobody = bearer(make_token(f"n{uid}@x.com"))
    r = signed_client.post("/projects", headers=nobody, json={"project_id": pid})
    assert r.status_code == 403 and r.json() == {"detail": "missing permission project.create"}
    assert signed_client.post("/projects", headers=creator, json={}).status_code == 400
    assert signed_client.post("/projects", headers=creator, json={"id": pid, "source": "s" * 65}).status_code == 400
    assert signed_client.post("/projects", headers=creator, json={"id": pid, "visibility": "secret"}).status_code == 400
    r = signed_client.post("/projects", headers=creator,
                           json={"id": pid, "aliases": ["Alpha Billing"], "keywords": ["invoices"],
                                 "descriptor": "Billing service", "visibility": "semi-public", "source": "AI-SDLC"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == pid and body["visibility"] == "semi-public" and body["token_refresh_needed"] is True
    assert body["c4_registry"] == "written" and body["source"] == "AI-SDLC"
    assert kc.calls[-1] == ("POST", "/projects", {"id": pid, "visibility": "semi-public"}, True)
    # existing id: needs project.write; merges aliases; never re-registers at KC
    r = signed_client.post("/projects", headers=creator, json={"id": pid, "aliases": ["more"]})
    assert r.status_code == 403 and r.json()["detail"] == "missing permission project.write on this project"
    owner = bearer(make_token(f"c{uid}@x.com", permissions=["project.create"], projects={pid: "owner"}))
    n = len(kc.calls)
    r = signed_client.post("/projects", headers=owner, json={"id": pid, "aliases": ["more"]})
    assert r.status_code == 200 and r.json()["aliases"] == ["Alpha Billing", "more"]
    assert "token_refresh_needed" not in r.json() and len(kc.calls) == n
    # personal id
    r = signed_client.post("/projects", headers=creator, json={"id": f"personal-x{uid}"})
    assert r.status_code == 403


def test_create_kc_refusals(signed_client, kc, uid):
    creator = bearer(make_token(f"c{uid}@x.com", permissions=["project.create"]))
    kc.answer[("POST", "/projects")] = (409, {"error": "project exists"})
    r = signed_client.post("/projects", headers=creator, json={"id": f"dup{uid}"})
    assert r.status_code == 409 and r.json()["detail"] == "identity service refused: project exists"
    assert not projects.exists(f"dup{uid}")
    kc.answer[("POST", "/projects")] = (500, "boom")
    assert signed_client.post("/projects", headers=creator, json={"id": f"x{uid}"}).status_code == 502
    kc.answer[("POST", "/projects")] = (200, {})
    r = signed_client.post("/projects", headers=creator, json={"id": "graphify__bad"})
    assert r.status_code == 400 and not any(c[1].endswith("graphify__bad") for c in kc.calls)


def test_create_local_failure_withdraws_kc(signed_client, kc, uid, monkeypatch):
    creator = bearer(make_token(f"c{uid}@x.com", permissions=["project.create"]))

    def boom(*a, **k):
        raise ValueError("disk says no")

    monkeypatch.setattr(projects, "get_or_create_project", boom)
    r = signed_client.post("/projects", headers=creator, json={"id": f"wd{uid}"})
    assert r.status_code == 400 and r.json()["detail"] == "disk says no"
    assert ("DELETE", f"/projects/wd{uid}", None, True) in kc.calls


def test_list_get_registry_filtered(signed_client, kc, uid):
    a, b = f"la{uid}", f"lb{uid}"
    m = maint(uid)
    for pid in (a, b):
        assert signed_client.post("/projects", headers=m, json={"id": pid}).status_code == 200
    reader = bearer(make_token(f"r{uid}@x.com", projects={a: "readonly"}))
    lst = signed_client.get("/projects", headers=reader).json()
    assert lst["version"] == 2 and lst["store"]["table"] == "projects" and lst["store"]["db"] == "sqlite"
    assert a in lst["projects"] and b not in lst["projects"] and lst["projects"][a]["visibility"] == "private"
    assert b in signed_client.get("/projects", headers=m).json()["projects"]
    assert signed_client.get(f"/projects?source=nope{uid}", headers=m).json()["projects"] == {}
    got = signed_client.get(f"/projects/{a}", headers=reader)
    assert got.status_code == 200 and "counter" in got.json() and got.json()["visibility"] == "private"
    assert signed_client.get(f"/projects/{b}", headers=reader).status_code == 403
    assert signed_client.get(f"/projects/none{uid}", headers=m).status_code == 404
    reg = signed_client.get("/projects/registry", headers=reader).json()
    ids = [p["id"] for p in reg["projects"]]
    assert reg["source"] == "c4" and a in ids and b not in ids and reg["count"] == len(ids)
    assert set(reg["projects"][0]) == {"id", "role", "aliases", "keywords", "descriptor", "source"}


def test_visibility(signed_client, kc, uid):
    pid = f"vis{uid}"
    m = maint(uid)
    signed_client.post("/projects", headers=m, json={"id": pid})
    assert signed_client.put(f"/projects/none{uid}/visibility", headers=m, json={"visibility": "public"}).status_code == 404
    assert signed_client.put(f"/projects/{pid}/visibility", headers=m, json={"visibility": "x"}).status_code == 400
    assert signed_client.put("/projects/orchestrator/visibility", headers=m,
                             json={"visibility": "public"}).status_code == 400
    r = signed_client.put(f"/projects/{pid}/visibility", headers=m, json={"visibility": "public"})
    assert r.json() == {"project": pid, "visibility": "public", "token_refresh_needed": True}
    assert kc.calls[-1][:3] == ("PUT", f"/projects/{pid}/visibility", {"visibility": "public"})
    project_access.invalidate()
    assert signed_client.get(f"/projects/{pid}", headers=m).json()["visibility"] == "public"
    kc.answer[("PUT", f"/projects/{pid}/visibility")] = (403, {"error": "not yours"})
    r = signed_client.put(f"/projects/{pid}/visibility", headers=bearer(make_token(f"u{uid}@x.com")),
                          json={"visibility": "private"})
    assert r.status_code == 403
    kc.answer.clear()
    signed_client.put(f"/projects/{pid}/visibility", headers=m, json={"visibility": "private"})
    assert project_access.visibility(pid) == "private"


def test_delete(signed_client, kc, uid):
    pid = f"del{uid}"
    m = maint(uid)
    signed_client.post("/projects", headers=m, json={"id": pid, "visibility": "public"})
    signed_client.post("/vector/add", headers=m, json={"text": f"row {uid}", "project": pid})
    editor = bearer(make_token(f"e{uid}@x.com", projects={pid: "editable"}))
    r = signed_client.delete(f"/projects/{pid}?confirm=true", headers=editor)
    assert r.status_code == 403 and r.json()["detail"] == "missing permission project.delete on this project"
    owner = bearer(make_token(f"o{uid}@x.com", projects={pid: "owner"}))
    assert signed_client.delete(f"/projects/{pid}", headers=owner).status_code == 400
    kc.answer[("DELETE", f"/projects/{pid}")] = (404, {"error": "gone"})
    r = signed_client.delete(f"/projects/{pid}?confirm=true", headers=owner)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert f"kh_know_{pid}" in rep["collections_dropped"] and rep["project_access_deleted"] is True
    assert not projects.exists(pid)
    assert pid not in [p["id"] for p in signed_client.get("/projects/registry", headers=m).json()["projects"]]
    assert signed_client.delete("/projects/orchestrator?confirm=true", headers=m).status_code == 400


def test_open_mode_project_routes(client, uid):
    r = client.post("/projects", json={"id": f"open{uid}"})
    assert r.status_code == 200 and "token_refresh_needed" not in r.json()
    assert client.delete(f"/projects/open{uid}?confirm=true").status_code == 200


# --- project match (§2.10 / §3.10) --------------------------------------------------------------------


def _mk(client, h, pid, aliases=(), keywords=()):
    r = client.post("/projects", headers=h, json={"id": pid, "aliases": list(aliases), "keywords": list(keywords)})
    assert r.status_code == 200, r.text


def test_project_match_outcomes(fresh_store, signed_client, kc, uid):
    m = maint(uid)
    _mk(signed_client, m, "billing-core", aliases=["Billing Engine"], keywords=["invoices", "vat"])
    _mk(signed_client, m, "payments", aliases=["billing engine"], keywords=["refunds"])
    _mk(signed_client, m, "crm", keywords=["leads"])
    pm = lambda text, h=m: signed_client.get("/orchestrator/project_match", params={"text": text}, headers=h).json()  # noqa: E731

    r = pm("Please fix the Billing-Core invoices")
    assert r["best"] == "billing-core" and r["ambiguous"] is False and r["policy_project"] == "billing-core"
    assert r["matches"][0] == {"project": "billing-core", "matched_term": "billing core", "matched_on": "id",
                               "term_length": 12, "role": "project", "weak": False, "source": "c4"}
    r = pm("the billing engine is slow")
    assert r["best"] is None and r["ambiguous"] is True and r["count"] == 2 and r["weak_only"] is False
    r = pm("what about VAT?")
    assert r["best"] is None and r["weak_only"] is True and r["ambiguous"] is False
    assert r["policy_project"] == "every project"
    r = pm("hello there")
    assert r == {"text": "hello there", "count": 0, "best": None, "ambiguous": False, "weak_only": False,
                 "policy_project": "every project", "matches": []}
    # diacritics and punctuation fold
    assert pm("Ćrm léads!")["best"] == "crm"
    # visibility filter (sign-in on)
    reader = bearer(make_token(f"r{uid}@x.com", projects={"payments": "readonly"}))
    r = pm("the billing engine is slow", reader)
    assert r["best"] == "payments" and r["count"] == 1
    # helpers
    tok = auth._current.set(None)
    try:
        assert projects.resolve_project_name("billing engine") is None
        assert projects.resolve_project_name("refunds") == "payments"
        assert projects.canonical_or_none("crm") == "crm"
        assert projects.canonical_or_none("CRM leads") == "crm"
        assert projects.canonical_or_none("nothing") is None
        assert projects.canonical_or_none("") is None
    finally:
        auth._current.reset(tok)


def test_match_falls_back_to_table_when_c4_empty(fresh_store):
    projects.get_or_create_project("zeta", aliases=["zed"])
    hits = projects.match_projects("ask zed")
    assert hits and hits[0]["source"] == "manifest-fallback" and hits[0]["matched_on"] == "alias"


# --- /orchestrator/resolve, shortcut, fact, switch (§2.10) ----------------------------------------------


def test_resolve_shortcut_fact_switch(signed_client, kc, uid):
    m = maint(uid)
    pid = f"rt{uid}"
    _mk(signed_client, m, pid)
    member = bearer(make_token(f"u{uid}@x.com", projects={pid: "readonly"}))
    assert signed_client.post("/orchestrator/shortcut", headers=member,
                              json={"project_id": pid, "text": "x"}).status_code == 403
    r = signed_client.post("/orchestrator/shortcut", headers=m,
                           json={"project_id": pid, "text": f"deploy the routing service {uid}"})
    assert r.status_code == 200 and r.json()["id"] == f"shortcut__{pid}"
    r = signed_client.post("/orchestrator/fact", headers=m, json={"text": f"office wifi password policy {uid}"})
    assert r.status_code == 200
    res = signed_client.post("/orchestrator/resolve", headers=member,
                             json={"query": f"deploy the routing service {uid}", "k": 5}).json()
    assert res["decision"] == "route" and res["switch_to"] == pid
    assert res["binding"]["knowledge"] == f"kh_know_{pid}" and res["binding"]["log"] == f"kh_log_{pid}"
    res = signed_client.post("/orchestrator/resolve", headers=member,
                             json={"intent": f"office wifi password policy {uid}"}).json()
    assert res["decision"] == "answer" and res["answer"].startswith("office wifi")
    # unreadable route -> answer_common, candidate stripped
    other = bearer(make_token(f"o{uid}@x.com"))
    res = signed_client.post("/orchestrator/resolve", headers=other,
                             json={"query": f"deploy the routing service {uid}", "route_threshold": 0.5}).json()
    assert res["decision"] in ("answer_common", "answer")
    assert all(c.get("project_id") != pid for c in res["candidates"])
    r = signed_client.post("/orchestrator/resolve", headers=other, json={"query": "x", "session_source": pid})
    assert r.status_code == 403
    sw = signed_client.get(f"/orchestrator/switch/{pid}", headers=member).json()
    assert sw == {"session_source": pid, "binding": {"project_id": pid, "vector": f"kh_src_{pid}",
                                                     "knowledge": f"kh_know_{pid}", "graph": sw["binding"]["graph"],
                                                     "policy": f"kh_policy_{pid}", "findings": f"kh_find_{pid}",
                                                     "log": f"kh_log_{pid}"}}
    assert signed_client.get(f"/orchestrator/switch/{pid}", headers=other).status_code == 403
