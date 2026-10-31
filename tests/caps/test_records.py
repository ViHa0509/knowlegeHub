"""§6.6 records: PUT/PATCH/GET, revisions, If-Match / If-None-Match, final, query with decay and partitions."""
from __future__ import annotations

from app import sqldb
from app.caps import core, records
from tests.auth_helpers import enable_signin
from tests.caps.conftest import attach, new_project

MANIFEST = {
    "schemas": [{"name": "task", "version": 1,
                 "schema": {"type": "object", "properties": {"title": {"type": "string"}, "prio": {"type": "number"},
                                                             "due": {"type": "integer"}, "team": {"type": "string"}},
                            "required": ["title"]}}],
    "types": [
        {"type": "task", "kind_of": "record", "schema": "task",
         "index_fields": {"team": "s", "prio": "n", "due": "t"}},
        {"type": "strict", "kind_of": "record", "require_if_match": True},
        {"type": "fenced.rec", "kind_of": "record", "fenced": True},
        {"type": "secret.rec", "kind_of": "record", "read_roles": ["judge"], "write_roles": ["judge"]},
        {"type": "ev", "kind_of": "event"},
    ],
}


def _env(client):
    ns, projects, _ = attach(client, manifest=MANIFEST)
    return ns, projects[0]


def test_put_get_revisions(client):
    ns, p = _env(client)
    base = f"/v1/{ns}/records/task/t-1"
    r = client.put(base, json={"title": "a", "team": "red", "prio": 2})
    assert r.status_code == 201, r.text
    assert r.headers["ETag"] == '"1"'
    v = r.json()
    assert v["rev"] == 1 and v["state"] == "open" and v["project"] == p
    assert v["index"] == {"team": "red", "prio": 2.0, "due": None}
    r2 = client.put(base, json={"title": "b", "team": "blue"}, headers={"If-Match": '"1"'})
    assert r2.status_code == 200 and r2.json()["rev"] == 2
    stale = client.put(base, json={"title": "c"}, headers={"If-Match": '"1"'})
    assert stale.status_code == 409 and stale.json()["error"] == "write_conflict"
    assert stale.json()["current"] == 2 and stale.headers["ETag"] == '"2"'
    dup = client.put(base, json={"title": "x"}, headers={"If-None-Match": "*"})
    assert dup.status_code == 409 and dup.json()["error"] == "duplicate"
    g = client.get(base)
    assert g.status_code == 200 and g.json()["body"] == {"title": "b", "team": "blue"} and g.headers["ETag"] == '"2"'
    old = client.get(base, params={"rev": 1}).json()
    assert old["rev"] == 1 and old["body"]["title"] == "a" and old["index"]["team"] == "red"
    revs = client.get(base + ":revisions").json()
    assert [x["rev"] for x in revs["revisions"]] == [2, 1] and revs["current"] == 2
    assert client.get(f"/v1/{ns}/records/task/nope").status_code == 404
    assert client.get(f"/v1/{ns}/records/unknown/x").status_code == 404
    assert client.get(f"/v1/{ns}/records/ev/x").status_code == 404
    assert client.put(f"/v1/{ns}/records/task/bad id!", json={"title": "a"}).status_code in (404, 422)
    assert client.put(base, json={"title": "a"}, params={"state": "Bad"}).status_code == 422


def test_validation_size_and_project(client):
    ns, p = _env(client)
    bad = client.put(f"/v1/{ns}/records/task/v1", json={"prio": 1})
    assert bad.status_code == 422 and bad.json()["error"] == "validation_failed" and bad.json()["errors"]
    big = client.put(f"/v1/{ns}/records/task/v2", json={"title": "x" * (records.BODY_MAX + 10)})
    assert big.status_code == 413
    ok = client.put(f"/v1/{ns}/records/task/v3", json={"title": "a"}, params={"project": p})
    assert ok.status_code == 201
    move = client.put(f"/v1/{ns}/records/task/v3", json={"title": "a"}, params={"project": "other"})
    assert move.status_code == 422
    assert client.put(f"/v1/{ns}/records/task/v4", json={"title": "a"},
                      params={"project": "not-attached"}).status_code == 422


def test_require_if_match_fenced_final(client):
    ns, _ = _env(client)
    s = f"/v1/{ns}/records/strict/s1"
    assert client.put(s, json={"a": 1}).status_code == 428
    assert client.put(s, json={"a": 1}, headers={"If-None-Match": "*"}).status_code == 201
    assert client.put(s, json={"a": 2}, headers={"If-Match": "1"}).status_code == 200
    assert client.patch(s, json={"b": 1}).status_code == 428
    f = f"/v1/{ns}/records/fenced.rec/f1"
    assert client.put(f, json={}).status_code == 428
    lease = client.post(f"/v1/{ns}/leases/w:acquire", json={}).json()
    h = {"X-KH-Lease": "w", "X-KH-Epoch": str(lease["epoch"])}
    assert client.put(f, json={}, headers=h).status_code == 201
    stale = client.put(f, json={}, headers={"X-KH-Lease": "w", "X-KH-Epoch": "99"})
    assert stale.status_code == 409 and stale.json()["error"] == "stale_epoch"
    fin = f"/v1/{ns}/records/task/fin"
    assert client.put(fin, json={"title": "a"}, params={"state": "final"}).status_code == 201
    r = client.put(fin, json={"title": "b"})
    assert r.status_code == 409 and r.json()["error"] == "record_final"
    assert client.patch(fin, json={"title": "c"}).json()["error"] == "record_final"


def test_merge_patch(client):
    ns, _ = _env(client)
    u = f"/v1/{ns}/records/task/m1"
    client.put(u, json={"title": "a", "team": "red", "prio": 1})
    r = client.patch(u, json={"team": None, "prio": 5}, params={"state": "doing"})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["body"] == {"title": "a", "prio": 5} and v["rev"] == 2 and v["state"] == "doing"
    assert v["index"]["team"] is None and v["index"]["prio"] == 5.0
    assert client.patch(u, json={"title": "z"}, headers={"If-Match": "1"}).status_code == 409
    assert client.patch(f"/v1/{ns}/records/task/none", json={"a": 1}).status_code == 404
    assert records.merge_patch({"a": {"b": 1, "c": 2}}, {"a": {"b": None}, "d": [1]}) == {"a": {"c": 2}, "d": [1]}


def test_query(client):
    ns, p = _env(client)
    for i, team in enumerate(["red", "red", "blue", "green", "red"]):
        client.put(f"/v1/{ns}/records/task/q{i}", json={"title": f"t{i}", "team": team, "prio": i})
    with sqldb.connect() as c:  # age q0 by two days
        c.execute("UPDATE kh_record SET updated_ms=updated_ms-? WHERE ns=? AND id='q0'", (2 * 86_400_000, ns))
    q = f"/v1/{ns}/records/task:query"
    r = client.post(q, json={"where": {"team": "red"}, "order": "prio"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert [x["id"] for x in out["records"]] == ["q0", "q1", "q4"] and out["count"] == 3 and out["type"] == "task"
    assert out["records"][0]["index"]["team"] == "red" and out["now_ms"] > 0
    r = client.post(q, json={"where": {"prio": {"gte": 1, "lt": 4}, "team": {"in": ["red", "blue"]}},
                             "order": ["-prio"]})
    assert [x["id"] for x in r.json()["records"]] == ["q2", "q1"]
    r = client.post(q, json={"where": {"id": {"ne": "q0"}}, "limit": 2, "offset": 1, "order": "id"})
    assert [x["id"] for x in r.json()["records"]] == ["q2", "q3"]
    d = client.post(q, json={"where": {"id": "q0"}, "decay": {"field": "updated_ms", "half_life_days": 2}}).json()
    rec = d["records"][0]
    assert 1.9 < rec["age_days"] < 2.1 and 0.45 < rec["weight"] < 0.55
    pt = client.post(q, json={"partition_by": "team", "per_partition": 1, "order": "prio"}).json()
    parts = {x["key"]: x for x in pt["partitions"]}
    assert parts["red"]["n"] == 3 and parts["red"]["ids"] == ["q0"] and parts["red"]["weight_sum"] == 3.0
    assert pt["count"] == 3
    assert client.post(q, json={"where": {"nope": 1}}).status_code == 422
    assert client.post(q, json={"where": {"team": {"like": "x"}}}).status_code == 422
    assert client.post(q, json={"limit": 501}).status_code == 422 or \
        client.post(q, json={"limit": 501}).json()["limit"] == 500
    assert client.post(q, json={"order": ["a", "b", "c", "d", "e"]}).status_code == 422
    assert client.post(q, json={"decay": {"field": "prio", "half_life_days": 1}}).status_code == 422
    assert client.post(q, json={"where": {"state": "open"}, "state": "open"}).json()["count"] == 5


def test_roles_and_acl(monkeypatch, client):
    ns, _ = _env(client)
    client.post(f"/v1/{ns}/principals", json={"role": "grader", "principal_id": "g1"})
    tok = client.post(f"/v1/{ns}/principals/g1/token").json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    client.put(f"/v1/{ns}/records/task/r1", json={"title": "a"})
    enable_signin(monkeypatch)
    assert client.get(f"/v1/{ns}/records/task/r1", headers=h).status_code == 200
    assert client.put(f"/v1/{ns}/records/task/r2", json={"title": "a"}, headers=h).status_code == 403
    assert client.get(f"/v1/{ns}/records/secret.rec/x", headers=h).status_code == 403


def test_type_usage_and_tables(client):
    ns, _ = _env(client)
    client.put(f"/v1/{ns}/records/task/u1", json={"title": "a"})
    m = {**MANIFEST, "types": [t for t in MANIFEST["types"] if t["type"] != "task"],
         "schemas": MANIFEST["schemas"]}
    r = client.put(f"/v1/{ns}/manifest", json=m, headers={"If-Match": '"1"'})
    assert r.status_code == 409 and r.json()["error"] == "type_in_use"
    with sqldb.connect() as c:
        assert records._usage(c, ns, "task") == 1
        n = c.execute("SELECT COUNT(*) FROM kh_record_rev WHERE ns=?", (ns,)).fetchone()[0]
    assert n == 1


def test_approval_records_readable(client):
    ns, _ = attach(client, projects=[new_project()])[:2]
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        sqldb.insert_new(c, "kh_record", {"ns": ns, "type": "kh.approval", "id": "ap-x", "project": "", "rev": 1,
                                          "state": "open", "ix_s1": "deploy/prod", "ix_s2": "a@b.c",
                                          "ix_s3": "approve", "body_json": core.dumps({"key": "deploy/prod"}),
                                          "actor_json": "{}", "created_ms": now, "updated_ms": now})
    g = client.get(f"/v1/{ns}/records/kh.approval/ap-x")
    assert g.status_code == 200 and g.json()["index"]["decision"] == "approve"
    assert client.put(f"/v1/{ns}/records/kh.approval/ap-x", json={}).status_code == 403
