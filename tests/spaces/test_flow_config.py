"""KH 7.1 flow editor routes: catalogs, expression check, event fire, flow and project-type edits."""
from __future__ import annotations

import pytest


def test_catalogs(client, fresh_store):
    ev = client.get("/flows/events").json()
    assert set(ev) >= {"sources", "listener"}
    nd = client.get("/flows/nodes").json()
    assert set(nd) >= {"nodes", "blocks", "pseudo", "ops_by"}


def _check(client, expr, kind="when", upstream=()):
    r = client.post("/flows/expr/check", json={"expr": expr, "kind": kind, "upstream": list(upstream)})
    assert r.status_code == 200
    return r.json()


def test_expr_check(client, fresh_store):
    assert _check(client, "") == {"ok": False, "reads": [], "error": "empty expression"}
    ok = _check(client, 'pl.col("a") > 1', upstream=["a"])
    assert ok == {"ok": True, "reads": ["a"], "error": ""}
    bad = _check(client, 'pl.col("b") > 1', upstream=["a"])
    assert bad["ok"] is False and bad["error"] == 'reads "b" which no upstream step gives'
    syn = _check(client, 'pl.col("a") >')
    assert syn["ok"] is False and syn["error"].startswith("not a Python/Polars expression")
    assert _check(client, "x > 1")["error"].startswith("unknown name 'x'")
    assert _check(client, "1", kind="other")["error"] == "kind must be one of filter, when"


def test_scope_events_runs(client, fresh_store):
    assert client.get("/flows/scope-events/runs").status_code == 422
    j = client.get("/flows/scope-events/runs", params={"type": "project"}).json()
    assert j["type"] == "project" and j["runs"] == [] and j["every_s"] == 300 and isinstance(j["spaces"], list)


def test_fire(client, fresh_store):
    j = client.post("/flows/events/kh.daily_0200/fire", params={"flow": "task.full-retrieve"}).json()
    assert j["queued"] is True and j["run_id"]
    j = client.post("/flows/events/kh.daily_0200/fire", params={"flow": "save.spec"}).json()
    assert j["queued"] is False and j["reason"]
    assert client.post("/flows/events/kh.daily_0200/fire", params={"flow": "nope"}).status_code == 404
    assert client.post("/flows/events/kh.agent_call/fire").status_code == 400


def test_save_read_back_and_delete(client, fresh_store):
    seed = client.get("/flows/task.full-retrieve").json()
    rev0 = client.get("/flows").json()["rev"]
    body = {k: v for k, v in seed.items() if k not in ("runnable", "id")}
    body["description"] = "An edited description"
    r = client.put("/flows/task.full-retrieve", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == seed["version"] + 1
    assert client.get("/flows/task.full-retrieve").json()["description"] == "An edited description"
    lst = client.get("/flows").json()
    assert lst["rev"] != rev0
    assert [f for f in lst["flows"] if f["id"] == "task.full-retrieve"][0]["description"] == "An edited description"
    d = client.delete("/flows/task.full-retrieve").json()
    assert d == {"id": "task.full-retrieve", "result": "reset"}
    assert client.get("/flows/task.full-retrieve").json()["description"] == seed["description"]
    assert client.get("/flows").json()["rev"] == rev0


def test_new_flow_and_search_cache(client, fresh_store):
    assert client.put("/flows/my.flow", json={}).status_code == 400
    assert client.put("/flows/my.flow", json={"name": "X", "id": "other"}).status_code == 400
    client.get("/flows", params={"q": "deploy"})  # warm the vector cache
    r = client.put("/flows/my.flow", json={"name": "Brand new flow", "description": "zzz unique", "kind": "task",
                                           "triggers": [{"type": "prompt", "examples": ["zzz unique"]}]})
    assert r.status_code == 200 and r.json()["version"] == 1
    ids = [f["id"] for f in client.get("/flows", params={"q": "zzz unique", "k": 50}).json()["flows"]]
    assert "my.flow" in ids
    assert client.delete("/flows/my.flow").json()["result"] == "deleted"
    assert client.get("/flows/my.flow").status_code == 404
    assert client.delete("/flows/my.flow").json()["result"] == "missing"


def test_system_flow_not_deletable(client, fresh_store):
    r = client.delete("/flows/sys.on_stop")
    assert r.status_code == 409
    assert client.delete("/flows/sys.on_session_start").status_code == 409


def test_project_types(client, fresh_store):
    assert client.put("/project-types/x1", json={}).status_code == 400
    r = client.put("/project-types/team", json={"name": "Team", "version": 2})
    assert r.status_code == 200 and r.json()["id"] == "team"
    assert "team" in [t["id"] for t in client.get("/project-types").json()["types"]]
    assert client.get("/project-types/team").json()["name"] == "Team"
    assert client.delete("/project-types/team").json()["result"] == "deleted"
    assert client.get("/project-types/team").status_code == 404
    assert client.put("/project-types/profile", json={"name": "Mine"}).status_code == 200
    assert client.get("/project-types/profile").json()["name"] == "Mine"
    assert client.delete("/project-types/profile").json()["result"] == "reset"
    assert client.get("/project-types/profile").json()["name"] == "Profile"
