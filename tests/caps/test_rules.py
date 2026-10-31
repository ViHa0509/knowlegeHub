"""§6.7 rules: write/dedupe/confirm, list, bulk, query (graph + sql), conflicts, scans, gate, reproject."""
from __future__ import annotations

from app import sqldb
from app.caps import rules
from tests.auth_helpers import enable_signin
from tests.caps.conftest import attach


def _rule(entity="Invoice", attribute="approval", cond="amount > 1000", effect="needs CFO approval", **kw):
    return {"entity": entity, "attribute": attribute, "condition": cond, "effect": effect,
            "quote_id": "q1", "source_text": "Invoices above 1000 need the CFO.", **kw}


def _tok(client, ns, pid, role):
    client.post(f"/v1/{ns}/principals", json={"role": role, "principal_id": pid})
    return {"Authorization": f"Bearer {client.post(f'/v1/{ns}/principals/{pid}/token').json()['access_token']}"}


def test_normalization_and_hash():
    assert rules.fold("  Café-Owner_ID ") == "cafe owner id"
    assert rules.squash("  A   B\tC ") == "a b c"
    h1 = rules.rule_hash("p", "invoice", "approval", "Amount  > 1000", "Needs CFO")
    assert h1 == rules.rule_hash("p", "invoice", "approval", "amount > 1000", "needs cfo")
    assert h1 != rules.rule_hash("q", "invoice", "approval", "amount > 1000", "needs cfo")


def test_overlap_kinds():
    assert rules.overlap("", "x > 1") == "unconditional"
    assert rules.overlap("always", "x > 1") == "unconditional"
    assert rules.overlap("a and b", "b && a") == "same"
    assert rules.overlap("a", "a; b") == "subsumes"
    assert rules.overlap("amount greater than 1000", "amount at most 500") is None
    assert rules.overlap("amount > 1000", "amount < 2000") == "possible"
    assert rules.overlap("amount at least 10", "amount below 10") is None
    assert rules.overlap("amount at most 10", "amount at least 10") == "possible"
    assert rules.overlap("country is fr", "country is not fr") is None
    assert rules.overlap("not urgent", "urgent") is None
    assert rules.overlap("tier = gold", "tier = silver") is None
    assert rules.overlap("tier = gold", "region = eu") == "possible"


def test_put_get_duplicate_confirm(client):
    ns, (p,), _ = attach(client)
    r = client.put(f"/v1/{ns}/rules/r1", json=_rule(links=[{"relation": "DEPENDS_ON", "to_entity": "CFO"}]))
    assert r.status_code == 201, r.text
    v = r.json()
    assert v["entity_key"] == "invoice" and v["validity"] == "unverified" and v["project"] == p
    assert v["origin"] == "write_back" and v["rev"] == 1 and r.headers["ETag"] == '"1"'
    assert v["links"] == [{"relation": "DEPENDS_ON", "to_entity": "CFO", "to_key": "cfo"}]
    assert v["projection"] == "ok"
    dup = client.put(f"/v1/{ns}/rules/r2", json=_rule(effect="Needs  CFO approval"))
    assert dup.status_code == 409 and dup.json()["error"] == "duplicate" and dup.json()["existing_rule_id"] == "r1"
    g = client.get(f"/v1/{ns}/rules/r1")
    assert g.status_code == 200 and g.json()["condition"] == "amount > 1000"
    assert client.get(f"/v1/{ns}/rules/none").status_code == 404
    c = client.post(f"/v1/{ns}/rules/r1:confirm", json={"validity": "verified", "run_id": "run1"})
    assert c.status_code == 200 and c.json()["validity"] == "verified" and c.json()["rev"] == 2
    # content change resets a confirmed rule
    u = client.put(f"/v1/{ns}/rules/r1", json=_rule(effect="needs CEO approval"))
    assert u.status_code == 200 and u.json()["validity"] == "unverified" and u.json()["rev"] == 3
    assert client.put(f"/v1/{ns}/rules/r1", json=_rule(), headers={"If-Match": '"1"'}).status_code == 409
    bad = client.put(f"/v1/{ns}/rules/r3", json={"entity": "x"})
    assert bad.status_code == 422 and bad.json()["error"] == "validation_failed"
    assert client.put(f"/v1/{ns}/rules/r3", json=_rule(entity="Z", superseded_by="r1")).status_code == 422
    assert client.put(f"/v1/{ns}/rules/r3", json=_rule(entity="Z", confidence=1.5)).status_code == 422
    assert client.put(f"/v1/{ns}/rules/r3", json=_rule(entity="Z", source_text="x" * 16001)).status_code == 422
    assert client.put(f"/v1/{ns}/rules/r3", json=_rule(
        entity="Z", links=[{"relation": "AFFECTS", "to_entity": f"e{i}"} for i in range(51)])).status_code == 422
    sup = client.put(f"/v1/{ns}/rules/r4", json=_rule(entity="Old", validity="superseded", superseded_by="r1"))
    assert sup.status_code == 201 and sup.json()["superseded_by"] == "r1"
    assert client.post(f"/v1/{ns}/rules/r1:confirm", json={"validity": "bogus"}).status_code == 422


def test_list_and_run_filter(client):
    ns, _, _ = attach(client)
    client.put(f"/v1/{ns}/rules/a", json=_rule(run_id="run-a"))
    client.put(f"/v1/{ns}/rules/b", json=_rule(entity="Order", effect="x"))
    client.put(f"/v1/{ns}/rules/c", json=_rule(entity="Order", effect="y", validity="retired"))
    assert {r["rule_id"] for r in client.get(f"/v1/{ns}/rules").json()["rules"]} == {"a", "b", "c"}
    assert [r["rule_id"] for r in client.get(f"/v1/{ns}/rules", params={"entity": "ORDER",
                                                                      "validity": "unverified"}).json()["rules"]] == ["b"]
    assert [r["rule_id"] for r in client.get(f"/v1/{ns}/rules", params={"run_id": "run-a"}).json()["rules"]] == ["a"]
    assert client.get(f"/v1/{ns}/rules", params={"validity": "nope"}).status_code == 422
    assert client.get(f"/v1/{ns}/rules", params={"limit": 1}).json()["count"] == 1


def test_bulk(client):
    ns, _, _ = attach(client)
    body = {"run_id": "b1", "rules": [_rule(), _rule(effect="needs  CFO approval"), _rule(entity="Order"),
                                      {"entity": "bad"}]}
    r = client.post(f"/v1/{ns}/rules:bulk", json=body)
    assert r.status_code == 422 and r.json()["rejected"][0]["index"] == 3
    r = client.post(f"/v1/{ns}/rules:bulk", params={"partial": "true"}, json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["created"] == 2 and out["duplicates"] == 1 and len(out["rejected"]) == 1
    assert out["projection"] == "ok"
    rid = out["accepted"][0]["rule_id"]
    assert rid.startswith("r_") and len(rid) == 26
    again = client.post(f"/v1/{ns}/rules:bulk", json={"run_id": "b2", "rules": [_rule()]}).json()
    assert again["created"] == 0 and again["duplicates"] == 1 and again["accepted"][0]["rule_id"] == rid
    assert [x["rule_id"] for x in client.get(f"/v1/{ns}/rules", params={"run_id": "b2"}).json()["rules"]] == [rid]
    too_many = client.post(f"/v1/{ns}/rules:bulk", json={"rules": [_rule()] * 5001})
    assert too_many.status_code == 413


def _seed_graph(client, ns):
    client.put(f"/v1/{ns}/rules/r1", json=_rule(links=[{"relation": "DEPENDS_ON", "to_entity": "Vendor"}]))
    client.put(f"/v1/{ns}/rules/r2", json=_rule(cond="amount > 5000", effect="needs board approval"))
    client.put(f"/v1/{ns}/rules/r3", json=_rule(cond="amount < 100", effect="auto approve"))
    client.put(f"/v1/{ns}/rules/r4", json=_rule(entity="Vendor", attribute="status", cond="", effect="must be active",
                                                links=[{"relation": "AFFECTS", "to_entity": "Payment"}]))
    client.put(f"/v1/{ns}/rules/r5", json=_rule(entity="Payment", attribute="x", effect="y"))
    client.put(f"/v1/{ns}/rules/r6", json=_rule(entity="Invoice", attribute="old", effect="z", validity="retired"))


def test_query_graph_and_sql(client):
    ns, (p,), _ = attach(client)
    _seed_graph(client, ns)
    for path in ("auto", "sql"):
        q = client.post(f"/v1/{ns}/rules:query", json={"start_entities": ["invoice"], "hops": 1, "path": path})
        assert q.status_code == 200, q.text
        out = q.json()
        assert out["path"] == ("graph" if path == "auto" else "sql") and "fallback_reason" not in out
        assert [r["rule_id"] for r in out["rules"]] == ["r1", "r2", "r3"]
        pairs = {tuple(c["rule_ids"]): c["overlap"] for c in out["conflicts"]}
        assert pairs == {("r1", "r2"): "possible"}
        assert out["related"] == ["vendor"] and out["truncated"] is False
        assert {"rule_id": "r1", "relation": "DEPENDS_ON", "entity_key": "vendor"} in out["edges"]
    deep = client.post(f"/v1/{ns}/rules:query", json={"start_entities": ["Invoice"], "hops": 3}).json()
    assert [r["rule_id"] for r in deep["rules"]] == ["r1", "r2", "r3", "r4", "r5"]
    sql = client.post(f"/v1/{ns}/rules:query", json={"start_entities": ["Invoice"], "hops": 3, "path": "sql"}).json()
    assert [r["rule_id"] for r in sql["rules"]] == ["r1", "r2", "r3", "r4", "r5"]
    dead = client.post(f"/v1/{ns}/rules:query", json={"start_entities": ["Invoice"],
                                                     "validity_in": ["retired"]}).json()
    assert [r["rule_id"] for r in dead["rules"]] == ["r6"]
    only_applies = client.post(f"/v1/{ns}/rules:query", json={"start_entities": ["Payment"],
                                                             "relations": ["APPLIES_TO"]}).json()
    assert [r["rule_id"] for r in only_applies["rules"]] == ["r5"]
    assert client.post(f"/v1/{ns}/rules:query", json={"start_entities": ["a"], "hops": 4}).status_code == 422
    assert client.post(f"/v1/{ns}/rules:query", json={"start_entities": []}).status_code == 422
    # stale projection -> SQL with a reason; reproject fixes it
    with sqldb.connect() as c:
        c.execute("UPDATE kh_rule_proj SET state='stale' WHERE ns=?", (ns,))
    s = client.post(f"/v1/{ns}/rules:query", json={"start_entities": ["invoice"]}).json()
    assert s["path"] == "sql" and s["fallback_reason"] == "projection_stale"
    rp = client.post(f"/v1/{ns}/rules:reproject", json={"project": p})
    assert rp.status_code == 200 and rp.json()["state"] == "ok" and rp.json()["rules"] == 6
    assert client.post(f"/v1/{ns}/rules:query", json={"start_entities": ["invoice"]}).json()["path"] == "graph"


def test_projection_off_and_cron(client, monkeypatch):
    ns, (p,), _ = attach(client)
    monkeypatch.setenv("KH_RULES_GRAPH", "false")
    r = client.put(f"/v1/{ns}/rules/r1", json=_rule())
    assert r.json()["projection"] == "off"
    q = client.post(f"/v1/{ns}/rules:query", json={"start_entities": ["invoice"]}).json()
    assert q["path"] == "sql" and q["fallback_reason"] == "graph_off"
    monkeypatch.setenv("KH_RULES_GRAPH", "true")
    with sqldb.connect() as c:
        sqldb.upsert(c, "kh_rule_proj", ["ns", "project"], {"ns": ns, "project": p, "state": "stale",
                                                             "error": None, "updated_ms": 0})
    assert client.put(f"/v1/{ns}/rules/r2", json=_rule(entity="B")).json()["projection"] == "stale"
    assert rules.reproject_stale() >= 1
    assert client.post(f"/v1/{ns}/rules:query", json={"start_entities": ["b"]}).json()["path"] == "graph"


def test_lifecycle_hooks_drop_and_rebuild_graph(client, monkeypatch):
    ns, (p,), _ = attach(client)
    _seed_graph(client, ns)
    dropped = []
    monkeypatch.setattr("app.graph_db.drop_project_graph", lambda pid: dropped.append(pid) or True)
    rules.purge_ns(ns)
    assert dropped == [f"{ns}__{p}"]
    monkeypatch.undo()
    with sqldb.connect() as c:
        c.execute("UPDATE kh_rule_proj SET state='stale' WHERE ns=?", (ns,))
    rules.rebuild_ns(ns)
    assert client.post(f"/v1/{ns}/rules:query", json={"start_entities": ["invoice"]}).json()["path"] == "graph"


def test_scans(client):
    ns, (p,), _ = attach(client)
    client.put(f"/v1/{ns}/rules/r1", json=_rule())
    client.put(f"/v1/{ns}/rules/r2", json=_rule(entity="Order", effect="x"))
    client.post(f"/v1/{ns}/rules/r2:confirm", json={"validity": "verified"})
    body = {"scan_id": "s1", "entities_scanned": ["Invoice", "Order", "Vendor"]}
    r = client.post(f"/v1/{ns}/rules/scans", json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["n_scanned"] == 3 and out["n_any"] == 2 and out["n_verified"] == 1
    assert out["coverage_any"] == 0.666667 and out["coverage_verified"] == 0.333333
    assert out["uncovered"] == ["Vendor"] and out["unverified_only"] == ["Invoice"]
    replay = client.post(f"/v1/{ns}/rules/scans", json={**body, "entities_scanned": ["order", "vendor", "INVOICE"]})
    assert replay.status_code == 200 and replay.json()["n_any"] == 2
    other = client.post(f"/v1/{ns}/rules/scans", json={**body, "entities_scanned": ["x"]})
    assert other.status_code == 409
    lst = client.get(f"/v1/{ns}/rules/scans", params={"project": p}).json()
    assert lst["count"] == 1 and "uncovered" not in lst["scans"][0]
    assert client.get(f"/v1/{ns}/rules/scans/s1").json()["uncovered"] == ["Vendor"]
    assert client.get(f"/v1/{ns}/rules/scans/nope").status_code == 404


def test_gate(client):
    ns, _, _ = attach(client)
    client.put(f"/v1/{ns}/rules/r1", json=_rule(run_id="g1", links=[{"relation": "DEPENDS_ON", "to_entity": "Vendor"}]))
    nosrc = _rule(entity="Order", effect="x", run_id="g1")
    nosrc.pop("quote_id")
    nosrc.pop("source_text")
    client.put(f"/v1/{ns}/rules/r2", json=nosrc)
    g = client.post(f"/v1/{ns}/rules:gate", json={"run_id": "g1", "entities": ["Invoice", "Order", "Payment"]})
    assert g.status_code == 200, g.text
    out = g.json()
    assert out["pass"] is False and out["missing"] == ["Payment"]
    assert {"rule_id": "r2", "reason": "no_source"} in out["orphans"]
    assert {"rule_id": "r1", "reason": "dangling_dependency", "to_entity": "Vendor"} in out["orphans"]
    client.put(f"/v1/{ns}/rules/r3", json=_rule(entity="Vendor", effect="v"))
    client.put(f"/v1/{ns}/rules/r2", json=_rule(entity="Order", effect="x", run_id="g1"))
    ok = client.post(f"/v1/{ns}/rules:gate", json={"run_id": "g1", "entities": ["Invoice", "Order"]}).json()
    assert ok["pass"] is True and ok["orphans"] == [] and ok["n_rules"] == 2
    # a rule confirmed in the run counts for it
    client.post(f"/v1/{ns}/rules/r3:confirm", json={"validity": "verified", "run_id": "g2"})
    assert client.post(f"/v1/{ns}/rules:gate", json={"run_id": "g2", "entities": ["Vendor"]}).json()["pass"] is True
    assert client.post(f"/v1/{ns}/rules:gate", json={"entities": []}).status_code == 422


def test_roles_policy(monkeypatch, client):
    ns, (p,), _ = attach(client, manifest={"settings": {"rules": {"require_if_match": True,
                                                                  "write_roles": ["agent"]}}})
    agent = _tok(client, ns, "ag1", "agent")
    judge = _tok(client, ns, "j1", "judge")
    conf = _tok(client, ns, "c1", "confirmer")
    orch = _tok(client, ns, "o1", "orchestrator")
    enable_signin(monkeypatch)
    assert client.put(f"/v1/{ns}/rules/r1", json=_rule(), headers=agent).status_code == 428
    assert client.put(f"/v1/{ns}/rules/r1", json=_rule(), headers={**agent, "If-None-Match": "*"}).status_code == 201
    assert client.put(f"/v1/{ns}/rules/r1", json=_rule(), headers={**agent, "If-None-Match": "*"}).status_code == 409
    assert client.put(f"/v1/{ns}/rules/r1", json=_rule(effect="b"), headers=agent).status_code == 428
    assert client.put(f"/v1/{ns}/rules/r1", json=_rule(effect="b"),
                      headers={**agent, "If-Match": '"1"'}).status_code == 200
    assert client.put(f"/v1/{ns}/rules/r2", json=_rule(entity="X", validity="verified"),
                      headers={**agent, "If-None-Match": "*"}).status_code == 403
    assert client.post(f"/v1/{ns}/rules/r1:confirm", json={"validity": "verified"}, headers=agent).status_code == 403
    assert client.put(f"/v1/{ns}/rules/r3", json=_rule(entity="Y"),
                      headers={**judge, "If-None-Match": "*"}).status_code == 403
    assert client.get(f"/v1/{ns}/rules/r1", headers=judge).status_code == 200
    c = client.post(f"/v1/{ns}/rules/r1:confirm", json={"validity": "contradicted"}, headers=conf)
    assert c.status_code == 200 and c.json()["validity"] == "contradicted"
    assert client.post(f"/v1/{ns}/rules:reproject", json={"project": p}, headers=agent).status_code == 403
    assert client.post(f"/v1/{ns}/rules:reproject", json={"project": p}, headers=orch).status_code == 200
