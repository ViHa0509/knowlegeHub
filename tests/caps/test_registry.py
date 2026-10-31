"""§6.2 registry: namespaces and manifests."""
from __future__ import annotations

import copy

from app import sqldb
from app.caps import core
from tests.caps.conftest import EVENT_MANIFEST, attach, new_ns, new_project
from tests.auth_helpers import bearer, enable_signin, make_token


def test_attach_get_update(client):
    p = new_project()
    ns = new_ns()
    r = client.put(f"/v1/admin/ns/{ns}", json={"projects": [p], "display": "Demo"})
    assert r.status_code == 201
    body = r.json()
    assert body["state"] == "active" and body["projects"] == [p] and body["rev"] == 1
    assert body["client"]["client_id"] == f"kh-ns-{ns}" and body["client"]["client_secret"]
    assert r.headers["ETag"] == '"1"'
    g = client.get(f"/v1/admin/ns/{ns}")
    assert g.status_code == 200 and g.json()["display"] == "Demo"
    u = client.put(f"/v1/admin/ns/{ns}", json={"config": {"a": 1}}, headers={"If-Match": '"1"'})
    assert u.status_code == 200 and u.json()["config"] == {"a": 1} and u.json()["projects"] == [p]
    u2 = client.put(f"/v1/admin/ns/{ns}", json={"config": {"b": 2}}, headers={"If-Match": '"1"'})
    assert u2.status_code == 409 and u2.json()["error"] == "write_conflict"
    u3 = client.put(f"/v1/admin/ns/{ns}", json={"config": {"b": 2}})
    assert u3.json()["config"] == {"a": 1, "b": 2}
    assert any(n["ns"] == ns for n in client.get("/v1/admin/ns").json()["namespaces"])


def test_attach_validation(client):
    assert client.put("/v1/admin/ns/admin", json={"projects": []}).status_code == 422
    assert client.put("/v1/admin/ns/khx_y", json={"projects": []}).status_code == 422
    assert client.put("/v1/admin/ns/Bad", json={"projects": []}).status_code == 422
    r = client.put(f"/v1/admin/ns/{new_ns()}", json={"projects": ["no-such-project-xyz"]})
    assert r.status_code == 422 and r.json()["error"] == "invalid"
    assert client.get(f"/v1/admin/ns/{new_ns()}").status_code == 404


def test_transitions(client):
    ns, _, _ = attach(client, manifest=EVENT_MANIFEST)
    ok = client.post(f"/v1/{ns}/events", json={"kind": "step.done", "body": {"n": 1}})
    assert ok.status_code == 200
    s = client.post(f"/v1/admin/ns/{ns}:suspend")
    assert s.status_code == 200 and s.json()["state"] == "suspended" and s.json()["from"] == "active"
    w = client.post(f"/v1/{ns}/events", json={"kind": "step.done", "body": {"n": 2}})
    assert w.status_code == 423 and w.json()["error"] == "ns_suspended"
    assert client.get(f"/v1/{ns}/events").status_code == 200
    assert client.post(f"/v1/admin/ns/{ns}:resume").json()["state"] == "active"
    # suspended -> detached allowed, detached -> suspended not
    assert client.delete(f"/v1/admin/ns/{ns}").json()["state"] == "detached"
    row = client.get(f"/v1/admin/ns/{ns}").json()
    assert row["detached_ms"] and row["purge_after_ms"] - row["detached_ms"] == 30 * 86_400_000
    assert client.get(f"/v1/{ns}/events").status_code == 404
    bad = client.post(f"/v1/admin/ns/{ns}:suspend")
    assert bad.status_code == 409 and bad.json()["error"] == "invalid_transition"
    again = client.post(f"/v1/admin/ns/{ns}:detach")
    assert again.status_code == 200 and again.json()["changed"] is False
    ra = client.post(f"/v1/admin/ns/{ns}:reattach")
    assert ra.status_code == 200 and ra.json()["state"] == "active" and ra.json()["purge_after_ms"] is None
    evs = client.get(f"/v1/{ns}/events", params={"kind": "kh.ns_state"}).json()["events"]
    assert [(e["body"]["from"], e["body"]["to"]) for e in evs][-4:] == [
        ("active", "suspended"), ("suspended", "active"), ("active", "detached"), ("detached", "active")]
    with sqldb.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM kh_event WHERE ns='_kh' AND kind='kh.ns_state' AND body_json LIKE ?",
                      (f'%"{ns}"%',)).fetchone()[0]
    assert n >= 4


def test_reattach_after_grace(client):
    ns, _, _ = attach(client)
    client.post(f"/v1/admin/ns/{ns}:detach")
    with sqldb.connect() as c:
        c.execute("UPDATE kh_ns SET purge_after_ms=1 WHERE ns=?", (ns,))
    core.invalidate_ns(ns)
    r = client.post(f"/v1/admin/ns/{ns}:reattach")
    assert r.status_code == 409 and r.json()["error"] == "invalid_transition"


def test_detach_bumps_leases(client):
    ns, _, _ = attach(client)
    a = client.post(f"/v1/{ns}/leases/job:acquire", json={"ttl_s": 60}).json()
    client.post(f"/v1/admin/ns/{ns}:suspend")
    client.post(f"/v1/admin/ns/{ns}:resume")
    lease = client.get(f"/v1/{ns}/leases/job").json()
    assert lease["epoch"] == a["epoch"] + 2 and lease["holder"] is None and lease["held"] is False


def test_purged_tombstone_reattach(client):
    ns, _, _ = attach(client)
    with sqldb.connect() as c:
        c.execute("UPDATE kh_ns SET state='purged', purge_after_ms=5 WHERE ns=?", (ns,))
    r = client.put(f"/v1/admin/ns/{ns}", json={"projects": []})
    assert r.status_code == 409
    with sqldb.connect() as c:
        c.execute("UPDATE kh_ns SET purge_after_ms=NULL WHERE ns=?", (ns,))
    r = client.put(f"/v1/admin/ns/{ns}", json={"projects": []})
    assert r.status_code == 201 and r.json()["manifest_rev"] == 0


def test_manifest_apply(client):
    ns, _, _ = attach(client)
    g = client.get(f"/v1/{ns}/manifest")
    assert g.json() == {"ns": ns, "manifest_rev": 0, "manifest": None}
    r = client.put(f"/v1/{ns}/manifest", json=EVENT_MANIFEST)
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] is True and body["manifest_rev"] == 1 and len(body["sha256"]) == 64
    assert body["types"] == sorted(t["type"] for t in EVENT_MANIFEST["types"])
    assert body["schemas"] == [{"name": "step", "version": 1}]
    assert r.headers["ETag"] == '"1"'
    # same manifest -> unchanged even without If-Match
    same = client.put(f"/v1/{ns}/manifest", json=EVENT_MANIFEST)
    assert same.json()["changed"] is False
    m2 = copy.deepcopy(EVENT_MANIFEST)
    m2["description"] = "v2"
    assert client.put(f"/v1/{ns}/manifest", json=m2).status_code == 428
    assert client.put(f"/v1/{ns}/manifest", json=m2, headers={"If-Match": '"0"'}).status_code == 409
    ok = client.put(f"/v1/{ns}/manifest", json=m2, headers={"If-Match": '"1"'})
    assert ok.status_code == 200 and ok.json()["manifest_rev"] == 2
    got = client.get(f"/v1/{ns}/manifest")
    assert got.json()["manifest"]["description"] == "v2" and got.headers["ETag"] == '"2"'


def test_manifest_errors(client):
    ns, _, _ = attach(client)
    bad = {"bogus": 1, "types": [{"type": "kh.x"}, {"type": "a", "kind_of": "nope"},
                                 {"type": "b", "schema": "missing"},
                                 {"type": "c", "kind_of": "event", "index_fields": {"t": "t"}}],
           "schemas": [{"name": "s", "version": 1, "schema": {"type": "nope"}}],
           "subscriptions": [{"sub_id": "x", "kinds": ["a"], "url": "http://example.com"}]}
    r = client.put(f"/v1/{ns}/manifest", json=bad)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "invalid_manifest"
    ptrs = {e["pointer"] for e in body["errors"]}
    assert {"/bogus", "/types/0/type", "/types/1/kind_of", "/types/2/schema", "/types/3/index_fields/t",
            "/schemas/0/schema", "/subscriptions/0/url"} <= ptrs


def test_manifest_schema_immutable_and_types(client):
    ns, _, _ = attach(client, manifest=EVENT_MANIFEST)
    m = copy.deepcopy(EVENT_MANIFEST)
    m["schemas"][0]["schema"]["properties"]["extra"] = {"type": "string"}
    r = client.put(f"/v1/{ns}/manifest", json=m, headers={"If-Match": "1"})
    assert r.status_code == 409 and r.json()["error"] == "schema_immutable"
    # kind_of cannot change
    m = copy.deepcopy(EVENT_MANIFEST)
    m["types"][0]["kind_of"] = "record"
    r = client.put(f"/v1/{ns}/manifest", json=m, headers={"If-Match": "1"})
    assert r.status_code == 422
    # type in use cannot be dropped without archive
    assert client.post(f"/v1/{ns}/events", json={"kind": "step.done", "body": {"n": 1}}).status_code == 200
    m = copy.deepcopy(EVENT_MANIFEST)
    m["types"] = [t for t in m["types"] if t["type"] != "step.done"]
    r = client.put(f"/v1/{ns}/manifest", json=m, headers={"If-Match": "1"})
    assert r.status_code == 409 and r.json()["error"] == "type_in_use"
    r = client.put(f"/v1/{ns}/manifest?archive=true", json=m, headers={"If-Match": "1"})
    assert r.status_code == 200 and "step.done" in r.json()["removed_types"]
    w = client.post(f"/v1/{ns}/events", json={"kind": "step.done", "body": {"n": 1}})
    assert w.status_code == 422  # archived type takes no writes
    # unused type is simply deleted
    m2 = copy.deepcopy(m)
    m2["types"] = [t for t in m2["types"] if t["type"] != "unit.write"]
    r = client.put(f"/v1/{ns}/manifest", json=m2, headers={"If-Match": "2"})
    assert r.status_code == 200 and r.json()["removed_types"] == ["unit.write"]


def test_index_columns_stable(client):
    ns, _, _ = attach(client, manifest={"types": [{"type": "r", "index_fields": {"a": "s", "b": "s", "x": "n"}}]})
    st = core.ns_state(ns, fresh=True)
    first = st.types["r"]["index_fields"]
    assert first == {"a": "ix_s1", "b": "ix_s2", "x": "ix_n1"}
    m = {"types": [{"type": "r", "index_fields": {"b": "s", "c": "s", "t": "t"}}]}
    assert client.put(f"/v1/{ns}/manifest", json=m, headers={"If-Match": "1"}).status_code == 200
    st = core.ns_state(ns, fresh=True)
    assert st.types["r"]["index_fields"] == {"b": "ix_s2", "c": "ix_s1", "t": "ix_t1"}
    m = {"types": [{"type": "r", "index_fields": {"b": "n"}}]}
    r = client.put(f"/v1/{ns}/manifest", json=m, headers={"If-Match": "2"})
    assert r.status_code == 422


def test_signed_in_management(monkeypatch, client):
    enable_signin(monkeypatch)
    p1, p2 = new_project(), new_project()
    owner = bearer(make_token("owner@x.com", projects={p1: "owner", p2: "editable"}))
    other = bearer(make_token("other@x.com", projects={p1: "editable"}))
    maint = bearer(make_token("boss@x.com", role="maintainer"))
    ns = new_ns()
    r = client.put(f"/v1/admin/ns/{ns}", json={"projects": [p1, p2]}, headers=owner)
    assert r.status_code == 403  # does not own p2
    r = client.put(f"/v1/admin/ns/{ns}", json={"projects": [p1]}, headers=owner)
    assert r.status_code == 201 and r.json()["owner_email"] == "owner@x.com"
    assert client.put(f"/v1/admin/ns/{ns}", json={"projects": [p1]}, headers=other).status_code == 403
    assert client.get(f"/v1/admin/ns/{ns}", headers=other).status_code == 403
    assert client.post(f"/v1/admin/ns/{ns}:suspend", headers=other).status_code == 403
    assert client.get(f"/v1/{ns}/manifest", headers=other).status_code == 403
    assert client.get(f"/v1/{ns}/manifest", headers=owner).status_code == 200
    assert client.get(f"/v1/{ns}/manifest", headers=maint).status_code == 200
    mine = [n["ns"] for n in client.get("/v1/admin/ns", headers=owner).json()["namespaces"]]
    assert ns in mine
    assert ns not in [n["ns"] for n in client.get("/v1/admin/ns", headers=other).json()["namespaces"]]
    assert client.post(f"/v1/admin/ns/{ns}:suspend", headers=maint).status_code == 200
    assert client.get("/v1/admin/ns").status_code == 401
