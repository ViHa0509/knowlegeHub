"""§6.13 metrics: write (dup/conflict, binary scale, validation), list, aggregate, compare, rollback."""
from __future__ import annotations

import math
import time

from app import sqldb
from app.caps import core, metrics
from tests.caps.conftest import attach

DAY = 86_400_000

MANIFEST = {
    "schemas": [{"name": "dims", "version": 1,
                 "schema": {"type": "object", "properties": {"arm": {"type": "string"}}}}],
    "types": [
        {"type": "succ", "kind_of": "metric"},
        {"type": "lat", "kind_of": "metric", "schema": "dims"},
        {"type": "hidden.m", "kind_of": "metric", "read_roles": ["judge"]},
    ],
    "settings": {"metrics": {"series": {"succ": {"scale": "binary", "dir": "up"}},
                             "rollback": {"min_n": 5, "window_days": 14}}},
}


def _ns(client):
    ns, projects, _ = attach(client, manifest=MANIFEST)
    return ns, projects[0]


def test_write_duplicate_conflict(client):
    ns, p = _ns(client)
    t0 = (int(time.time() * 1000) // DAY) * DAY
    r = client.post(f"/v1/{ns}/metrics", json={"series": "lat", "value": 1.5, "ts_ms": t0, "unit_id": "u1",
                                                "dims": {"arm": "a"}})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 1 and r.json()["accepted"][0]["status"] == "created"
    obs = [{"series": "lat", "value": 1.5, "ts_ms": t0, "unit_id": "u1"},
           {"series": "lat", "value": 9.0, "ts_ms": t0, "unit_id": "u1"},
           {"series": "lat", "value": 2.5, "ts_ms": t0 + 1000, "unit_id": "u2"}]
    r = client.post(f"/v1/{ns}/metrics", json={"observations": obs})
    j = r.json()
    assert [a["status"] for a in j["accepted"]] == ["duplicate", "conflict", "created"]
    assert j["duplicates"] == 1 and j["conflicts"] == 1 and j["created"] == 1
    assert j["accepted"][1]["stored_value"] == 1.5
    # list form + envelope header unit id
    r = client.post(f"/v1/{ns}/metrics", json=[{"series": "succ", "value": 1, "ts_ms": t0}],
                    headers={"X-KH-Unit": "u9", "X-KH-Run": "r1"})
    assert r.json()["accepted"][0]["unit_id"] == "u9"
    rows = client.get(f"/v1/{ns}/metrics", params={"series": "succ"}).json()["observations"]
    assert rows[0]["unit_id"] == "u9" and rows[0]["run_id"] == "r1" and rows[0]["project"] == p
    # kh.duplicate emitted once
    ev = client.get(f"/v1/{ns}/events", params={"kind": "kh.duplicate"}).json()["events"]
    assert any(e["body"].get("what") == "metric" for e in ev)
    # rollup counts only really inserted rows
    with sqldb.connect() as c:
        r = c.execute("SELECT n, vsum FROM kh_metric_day WHERE ns=? AND series='lat'", (ns,)).fetchone()
    assert int(r[0]) == 2 and float(r[1]) == 4.0


def test_write_validation_all_or_nothing(client):
    ns, _ = _ns(client)
    r = client.post(f"/v1/{ns}/metrics", json={"observations": [
        {"series": "succ", "value": 1, "unit_id": "a"},
        {"series": "succ", "value": 0.5, "unit_id": "b"},
        {"series": "nope", "value": 1},
        {"series": "lat", "value": "x"},
        {"series": "lat", "value": 1, "dims": {"arm": 3}},
        {"series": "lat", "value": 1, "dims": {f"k{i}": i for i in range(17)}},
    ]})
    assert r.status_code == 422 and r.json()["error"] == "validation_failed"
    idx = {x["index"] for x in r.json()["rejected"]}
    assert idx == {1, 2, 3, 4, 5}
    assert client.get(f"/v1/{ns}/metrics").json()["observations"] == []
    big = [{"series": "lat", "value": 1, "ts_ms": i} for i in range(metrics.BATCH_MAX + 1)]
    assert client.post(f"/v1/{ns}/metrics", json=big).status_code == 413


def test_list_filters(client):
    ns, _ = _ns(client)
    t0 = 1_700_000_000_000
    client.post(f"/v1/{ns}/metrics", json=[
        {"series": "lat", "value": 1, "ts_ms": t0, "unit_id": "a", "run_id": "r1"},
        {"series": "lat", "value": 2, "ts_ms": t0 + 10, "unit_id": "b", "run_id": "r2", "baseline": True},
        {"series": "lat", "value": 3, "ts_ms": t0 + 20, "unit_id": "c", "run_id": "r2"},
    ])
    g = lambda **q: client.get(f"/v1/{ns}/metrics", params=q).json()["observations"]  # noqa: E731
    assert [o["value"] for o in g(series="lat")] == [3, 2, 1]
    assert [o["value"] for o in g(run_id="r2")] == [3, 2]
    assert [o["value"] for o in g(baseline="true")] == [2]
    assert [o["value"] for o in g(**{"from": t0 + 5, "to": t0 + 20})] == [2]
    assert [o["value"] for o in g(unit_id="a")] == [1]
    assert len(g(limit=1, offset=1)) == 1
    assert client.get(f"/v1/{ns}/metrics", params={"series": "nope"}).status_code == 404


def test_aggregate_rollup_and_raw(client):
    ns, p = _ns(client)
    d0 = 19_000 * DAY
    obs = [{"series": "lat", "value": v, "ts_ms": d0 + i, "unit_id": f"u{i}", "dims": {"arm": "a" if i % 2 else "b"}}
           for i, v in enumerate([1, 2, 3, 4])]
    obs.append({"series": "lat", "value": 10, "ts_ms": d0 + DAY, "unit_id": "z", "run_id": "rx"})
    client.post(f"/v1/{ns}/metrics", json=obs)
    a = client.get(f"/v1/{ns}/metrics:aggregate", params={"series": "lat", "group_by": "day"}).json()
    assert a["source"] == "rollup" and a["group_by"] == "day"
    first = a["groups"][0]
    assert first["n"] == 4 and first["sum"] == 10 and first["mean"] == 2.5
    assert math.isclose(first["sd"], math.sqrt(5 / 3))
    a = client.get(f"/v1/{ns}/metrics:aggregate", params={"series": "lat", "group_by": "dims.arm"}).json()
    assert a["source"] == "raw"
    by = {g["key"]: g for g in a["groups"]}
    assert by["a"]["n"] == 2 and by["b"]["sum"] == 4 and by[None]["n"] == 1
    a = client.get(f"/v1/{ns}/metrics:aggregate", params={"series": "lat", "group_by": "run_id"}).json()
    assert {g["key"] for g in a["groups"]} == {None, "rx"}
    a = client.get(f"/v1/{ns}/metrics:aggregate", params={"series": "lat", "group_by": "project"}).json()
    assert a["groups"] == [{"key": p, "n": 5, "sum": 20.0, "mean": 4.0, "sd": a["groups"][0]["sd"]}]
    a = client.get(f"/v1/{ns}/metrics:aggregate",
                   params={"series": "lat", "group_by": "day", "from": d0 + DAY, "to": d0 + 2 * DAY}).json()
    assert [g["n"] for g in a["groups"]] == [1]
    assert client.get(f"/v1/{ns}/metrics:aggregate", params={"series": "lat", "group_by": "bad"}).status_code == 422


def test_stats_functions():
    # proportion: 30/100 vs 45/100
    a = [1.0] * 30 + [0.0] * 70
    b = [1.0] * 45 + [0.0] * 55
    r = metrics.compare_values(a, b)
    assert r["test"] == "proportion"
    p = 75 / 200
    z = (0.45 - 0.30) / math.sqrt(p * (1 - p) * (2 / 100))
    assert math.isclose(r["z"], z) and math.isclose(r["p"], math.erfc(abs(z) / math.sqrt(2)))
    assert math.isclose(r["rel_delta"], 0.5)
    # Mann-Whitney with ties: U_b computed by hand
    a, b = [1.0, 2.0, 2.0, 3.0], [2.0, 4.0, 5.0]
    r = metrics.compare_values(a, b)
    assert r["test"] == "mann_whitney"
    # ranks: 1->1, 2,2,2->3, 3->5, 4->6, 5->7; R_b = 3+6+7 = 16; U_b = 16 - 6 = 10; mu = 6
    var = 4 * 3 / 12 * (8 - (27 - 3) / (7 * 6))
    assert math.isclose(r["z"], (10 - 6) / math.sqrt(var))
    assert metrics.compare_values([], [1.0])["p"] is None


def test_compare_route(client):
    ns, _ = _ns(client)
    obs = [{"series": "succ", "value": 1 if i < 3 else 0, "ts_ms": 1000 + i, "unit_id": f"a{i}", "run_id": "A"}
           for i in range(10)]
    obs += [{"series": "succ", "value": 1 if i < 7 else 0, "ts_ms": 2000 + i, "unit_id": f"b{i}", "run_id": "B"}
            for i in range(10)]
    client.post(f"/v1/{ns}/metrics", json=obs)
    r = client.post(f"/v1/{ns}/metrics:compare", json={"series": "succ", "a": {"run_id": "A"}, "b": {"run_id": "B"}})
    j = r.json()
    assert r.status_code == 200, r.text
    assert j["n_a"] == 10 and j["n_b"] == 10 and j["test"] == "proportion"
    assert math.isclose(j["mean_a"], 0.3) and math.isclose(j["rel_delta"], (0.7 - 0.3) / 0.3)
    assert j["p"] < 0.1 and j["per_unit"] is False
    r = client.post(f"/v1/{ns}/metrics:compare", json={"series": "succ", "a": {"unit_ids": ["a0", "a1"]},
                                                         "b": {"run_id": "B"}, "test": "mann_whitney",
                                                         "per_unit": True})
    assert r.json()["n_a"] == 2 and r.json()["test"] == "mann_whitney"
    assert client.post(f"/v1/{ns}/metrics:compare",
                       json={"series": "succ", "a": {"bogus": 1}, "b": {}}).status_code == 422


def _make_item(ns: str, item_id: str, project: str, activated_ms: int) -> None:
    """Insert an active item with a guard metric (minimal kh_item if the items module is absent)."""
    with sqldb.connect() as c:
        if not sqldb.table_exists(c, "kh_item"):
            c.execute("CREATE TABLE IF NOT EXISTS kh_item (ns TEXT NOT NULL, item_id TEXT NOT NULL, project TEXT, "
                      "scope TEXT, status TEXT, status_reason TEXT, guard_metric TEXT, metric_dir TEXT, "
                      "activated_ms INTEGER, rev INTEGER NOT NULL DEFAULT 1, updated_ms INTEGER, "
                      "PRIMARY KEY (ns, item_id))")
        info = c.execute("PRAGMA table_info(kh_item)").fetchall()
        row = {"ns": ns, "item_id": item_id, "project": project, "scope": "project", "status": "active",
               "guard_metric": "succ", "metric_dir": "up", "activated_ms": activated_ms, "rev": 1}
        for r in info:
            name, typ, notnull, dflt = r[1], str(r[2]).upper(), r[3], r[4]
            if name not in row and notnull and dflt is None:
                row[name] = 0 if "INT" in typ or "REAL" in typ else ""
        sqldb.insert_new(c, "kh_item", row)


def test_rollback(client):
    ns, p = _ns(client)
    now = int(time.time() * 1000)
    _make_item(ns, "it_bad", p, now - DAY)
    _make_item(ns, "it_ok", p, now - DAY)
    obs = []
    for i in range(10):  # served units: bad item -> mostly failures
        u = f"t{i}"
        core.emit(ns, "kh.served", {"item_ids": ["it_bad"], "unit_id": u})
        obs.append({"series": "succ", "value": 1 if i < 2 else 0, "ts_ms": now - 1000 + i, "unit_id": u})
    for i in range(20):
        obs.append({"series": "succ", "value": 1 if i < 16 else 0, "ts_ms": now - 900 + i, "unit_id": f"c{i}"})
    assert client.post(f"/v1/{ns}/metrics", json=obs).json()["created"] == 30
    r = client.post(f"/v1/{ns}/metrics:rollback", json={"dry_run": True})
    j = r.json()
    assert r.status_code == 200, r.text
    res = {x["item_id"]: x for x in j["results"]}
    assert res["it_bad"]["fire"] is True and res["it_bad"]["n_treatment"] == 10
    assert res["it_ok"]["fire"] is False and res["it_ok"]["reason"] == "too_few_units"
    assert j["rolled_back"] == []
    r = client.post(f"/v1/{ns}/metrics:rollback", json={"item_ids": ["it_bad"]})
    assert r.json()["rolled_back"] == ["it_bad"]
    with sqldb.connect() as c:
        st = c.execute("SELECT status, status_reason FROM kh_item WHERE ns=? AND item_id='it_bad'", (ns,)).fetchone()
    assert st[0] == "withdrawn" and st[1] == "auto_rollback"
    ev = client.get(f"/v1/{ns}/events", params={"kind": "kh.item_rollback"}).json()["events"]
    assert ev and ev[0]["body"]["item_id"] == "it_bad"
    # lease shared with the cron: a held lease refuses the route
    with sqldb.connect() as c:
        sqldb.lease_acquire(c, ns, metrics.ROLLBACK_LEASE, "someone", 60_000)
    assert client.post(f"/v1/{ns}/metrics:rollback", json={}).status_code == 409
    with sqldb.connect() as c:
        sqldb.lease_release(c, ns, metrics.ROLLBACK_LEASE, "someone")


def test_type_usage_and_retention(client):
    ns, _ = _ns(client)
    now = int(time.time() * 1000)
    client.post(f"/v1/{ns}/metrics", json=[{"series": "lat", "value": 1, "ts_ms": now - 40 * DAY, "unit_id": "a"},
                                           {"series": "lat", "value": 2, "ts_ms": now, "unit_id": "b"}])
    m = dict(MANIFEST, types=[t for t in MANIFEST["types"] if t["type"] != "lat"])
    r = client.put(f"/v1/{ns}/manifest", json=m, headers={"If-Match": "1"})
    assert r.status_code == 409 and r.json()["error"] == "type_in_use"
    m2 = dict(MANIFEST, types=[dict(t, retention_days=30) if t["type"] == "lat" else t for t in MANIFEST["types"]])
    assert client.put(f"/v1/{ns}/manifest", json=m2, headers={"If-Match": "1"}).status_code == 200
    metrics.cron_retention()
    rows = client.get(f"/v1/{ns}/metrics", params={"series": "lat"}).json()["observations"]
    assert [o["unit_id"] for o in rows] == ["b"]
    with sqldb.connect() as c:
        n = c.execute("SELECT SUM(n) FROM kh_metric_day WHERE ns=? AND series='lat'", (ns,)).fetchone()[0]
    assert int(n) == 2  # rollup kept


def test_read_acl(client):
    ns, _ = _ns(client)
    assert any(c["name"] == "metrics" for c in client.get("/v1/capabilities").json()["capabilities"])
    assert client.get(f"/v1/{ns}/metrics", params={"series": "hidden.m"}).status_code == 200  # admin passes
