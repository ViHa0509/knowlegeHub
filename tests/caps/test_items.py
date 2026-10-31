"""§6.9 items: propose/merge/contest/update, lint, transitions with quorum, support + promotion, expiry, index."""
from __future__ import annotations

from app import sqldb
from app.caps import items
from tests.auth_helpers import enable_signin
from tests.caps.conftest import attach, new_project

ITEM_MANIFEST = {
    "schemas": [{"name": "lesson", "version": 1,
                 "schema": {"type": "object", "properties": {"severity": {"type": "string"}},
                            "additionalProperties": False}}],
    "types": [{"type": "lesson", "kind_of": "item", "schema": "lesson"},
              {"type": "voted", "kind_of": "item", "confirm_policy": {"roles": ["confirmer"], "quorum": 2}},
              {"type": "auto", "kind_of": "item", "confirm_policy": {"auto_after_evidence": 2}}],
    "keyspaces": [{"keyspace": "task", "dims": ["area", "step"]}],
}


def _env(client, n_projects=1, manifest=None):
    projects = [new_project() for _ in range(n_projects)]
    ns, _, _ = attach(client, projects=projects, manifest=manifest or ITEM_MANIFEST)
    return ns, projects


def _propose(client, ns, project, text, key=None, **kw):
    body = {"kind": "lesson", "keyspace": "task", "key": key or {"area": "build", "step": "compile"},
            "project": project, "text": text, **kw}
    return client.post(f"/v1/{ns}/items", json=body)


def _tok(client, ns, pid, role):
    client.post(f"/v1/{ns}/principals", json={"role": role, "principal_id": pid})
    return {"Authorization": f"Bearer {client.post(f'/v1/{ns}/principals/{pid}/token').json()['access_token']}"}


def test_key_parsing(client):
    ns, _ = _env(client)
    from app.caps import core

    st = core.ns_state(ns, fresh=True)
    a = items.key_of(st, "task", {"area": "build", "step": "compile"})
    b = items.key_of(st, "task", "area=build;step=compile")
    c = items.key_of(st, "task", '{"step": "compile", "area": "build"}')
    assert a[2] == b[2] == c[2] and len(a[2]) == 40
    w = items.key_of(st, "task", {"area": "build"})
    assert w[0] == {"area": "build", "step": "*"} and w[2] == a[3]


def test_propose_merge_contest_update(client):
    ns, (p,) = _env(client)
    r = _propose(client, ns, p, "Run the linter before every commit.", title="Lint first",
                 attrs={"severity": "high"}, provenance={"runs": ["r1"], "event_ids": ["e1"]})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["result"] == "accepted" and out["lint"] == []
    it = out["item"]
    assert it["item_id"].startswith("it_") and it["status"] == "proposed" and it["evidence_count"] == 1
    assert it["provenance"] == {"runs": ["r1"], "event_ids": ["e1"]} and r.headers["ETag"]
    # exact duplicate (normalized) -> merged, 200
    dup = _propose(client, ns, p, "run the linter   before every commit.", provenance={"runs": ["r2"]})
    assert dup.status_code == 200 and dup.json()["result"] == "merged" and dup.json()["with"] == it["item_id"]
    assert dup.json()["item"]["evidence_count"] == 2
    # invalid attrs / unknown kind / keyspace
    assert _propose(client, ns, p, "Run x.", attrs={"nope": 1}).status_code == 422
    assert client.post(f"/v1/{ns}/items", json={"kind": "zzz", "keyspace": "task", "key": {}, "text": "Run x."}
                       ).status_code == 422
    assert _propose(client, ns, p, "Run x.", key="bad=1").status_code == 422
    # lint problems reported, not blocking
    hedgy = _propose(client, ns, p, "Maybe the cache is stale, I think.", key={"area": "cache"})
    assert hedgy.status_code == 201 and hedgy.json()["lint"]
    # leak gate (text already masked, but a secret shape the pack does not mask still refuses)
    # contradicts -> new contested + target contested
    con = _propose(client, ns, p, "Skip the linter on hotfix commits.", contradicts=it["item_id"])
    assert con.status_code == 201 and con.json()["result"] == "contested"
    assert con.json()["item"]["status"] == "contested"
    assert client.get(f"/v1/{ns}/items/{it['item_id']}").json()["status"] == "contested"
    # updates rewrites a live item
    up = _propose(client, ns, p, "Run the linter and the formatter before every commit.", updates=it["item_id"])
    assert up.status_code == 201 and up.json()["result"] == "updated"
    assert up.json()["item"]["text"].startswith("Run the linter and the formatter")
    # list / filters / patterns
    lst = client.get(f"/v1/{ns}/items", params={"status": "contested,unproven"}).json()["items"]
    assert len(lst) == 3
    by_key = client.get(f"/v1/{ns}/items", params={"keyspace": "task", "key": "area=build;step=compile"}).json()
    assert len(by_key["items"]) == 2
    pats = client.get(f"/v1/{ns}/items:patterns", params={"keyspace": "task"}).json()["patterns"]
    top = pats[0]
    assert top["count"] == 2 and top["by_status"] == {"contested": 2} and top["kind"] == "lesson"
    assert client.get(f"/v1/{ns}/items/nope").status_code == 404


def test_scrub_and_near_duplicate(client):
    ns, (p,) = _env(client)
    r = _propose(client, ns, p, "Send the report to ops@example.com every Monday.")
    assert r.status_code == 201
    assert "[REDACTED:email]" in r.json()["item"]["text"]
    a = _propose(client, ns, p, "Restart the build agent when the queue is stuck.", key={"area": "ci"})
    b = _propose(client, ns, p, "Restart the build agent when the queue is stuck!", key={"area": "ci"})
    assert a.status_code == 201 and b.status_code == 200 and b.json()["result"] == "merged"


def test_lint_route(client):
    ns, _ = _env(client)
    ok = client.post(f"/v1/{ns}/items:lint", json={"text": "Never deploy on Friday."}).json()
    assert ok == {"ok": True, "errors": []}
    bad = client.post(f"/v1/{ns}/items:lint", json={"text": "The deploy probably fails. " + "x" * 400,
                                                    "guard_names": ["g1"]}).json()
    assert bad["ok"] is False and len(bad["errors"]) == 4


def test_transitions_and_quorum(client, monkeypatch):
    ns, (p,) = _env(client)
    it = _propose(client, ns, p, "Pin the base image digest.").json()["item"]
    iid = it["item_id"]
    bad = client.patch(f"/v1/{ns}/items/{iid}", json={"status": "flying"})
    assert bad.status_code == 422
    act = client.patch(f"/v1/{ns}/items/{iid}", json={"status": "active"})
    assert act.status_code == 200, act.text
    a = act.json()
    assert a["status"] == "active" and a["expires_ms"] - a["last_confirmed_ms"] == 90 * 86_400_000
    stale = client.patch(f"/v1/{ns}/items/{iid}", json={"status": "contested"}, headers={"If-Match": '"1"'})
    assert stale.status_code == 409
    other = _propose(client, ns, p, "Pin every image digest.", key={"area": "img"}).json()["item"]
    nosup = client.patch(f"/v1/{ns}/items/{iid}", json={"status": "superseded"})
    assert nosup.status_code == 422
    sup = client.patch(f"/v1/{ns}/items/{iid}", json={"status": "superseded", "superseded_by": other["item_id"]})
    assert sup.status_code == 200 and sup.json()["superseded_by"] == other["item_id"]
    wrong = client.patch(f"/v1/{ns}/items/{iid}", json={"status": "active"})
    assert wrong.status_code == 409 and wrong.json()["error"] == "invalid_transition"
    re_ = client.patch(f"/v1/{ns}/items/{iid}", json={"status": "unproven"})
    assert re_.status_code == 200 and re_.json()["status"] == "proposed" and re_.json()["superseded_by"] is None
    # lint failure blocks activation
    h = _propose(client, ns, p, "The cache might be stale.", key={"area": "c"}).json()["item"]
    lf = client.patch(f"/v1/{ns}/items/{h['item_id']}", json={"status": "active"})
    assert lf.status_code == 422 and lf.json()["error"] == "lint_failed"
    # quorum 2 for confirmers
    v = client.post(f"/v1/{ns}/items", json={"kind": "voted", "keyspace": "task", "key": {"area": "v"},
                                             "project": p, "text": "Tag every release."}).json()["item"]
    c1, c2 = _tok(client, ns, "conf1", "confirmer"), _tok(client, ns, "conf2", "confirmer")
    ag = _tok(client, ns, "ag1", "agent")
    enable_signin(monkeypatch)
    assert client.patch(f"/v1/{ns}/items/{v['item_id']}", json={"status": "active"}, headers=ag).status_code == 403
    first = client.patch(f"/v1/{ns}/items/{v['item_id']}", json={"status": "active"}, headers=c1)
    assert first.status_code == 202 and first.json()["pending"] == {"confirmations": 1, "quorum": 2}
    second = client.patch(f"/v1/{ns}/items/{v['item_id']}", json={"status": "active"}, headers=c2)
    assert second.status_code == 200 and second.json()["status"] == "active"
    # agent may propose but not withdraw
    assert client.patch(f"/v1/{ns}/items/{v['item_id']}", json={"status": "withdrawn"}, headers=ag).status_code == 403
    assert client.patch(f"/v1/{ns}/items/{v['item_id']}", json={"status": "withdrawn"}, headers=c1).status_code == 200


def test_support_promotion_and_auto(client):
    manifest = {**ITEM_MANIFEST, "settings": {"items": {"cross_after_projects": 2}}}
    ns, (p1, p2) = _env(client, 2, manifest)
    it = client.post(f"/v1/{ns}/items", json={"kind": "lesson", "keyspace": "task", "key": {"area": "g"},
                                              "project": p1, "text": "Cache the dependency folder.",
                                              "bound": "generic", "provenance": {"runs": ["ra"]}}).json()["item"]
    # support from p2 citing the same run -> not independent
    s1 = client.post(f"/v1/{ns}/items/{it['item_id']}:support", json={"project": p2, "run_id": "ra"}).json()
    assert s1["new_support"] is True and s1["promoted"] is False
    again = client.post(f"/v1/{ns}/items/{it['item_id']}:support", json={"project": p2, "run_id": "ra"}).json()
    assert again["new_support"] is False
    s2 = client.post(f"/v1/{ns}/items/{it['item_id']}:support", json={"project": p2, "run_id": "rb"}).json()
    assert s2["promoted"] is False  # p1's only run is cited by p2: one independent project so far
    s2 = client.post(f"/v1/{ns}/items/{it['item_id']}:support", json={"project": p1, "run_id": "rc"}).json()
    assert s2["promoted"] is True and s2["item"]["scope"] == "cross" and s2["item"]["evidence_count"] == 4
    assert sorted(s2["item"]["support_projects"]) == sorted([p1, p2])
    # auto_after_evidence
    au = client.post(f"/v1/{ns}/items", json={"kind": "auto", "keyspace": "task", "key": {"area": "a"},
                                              "project": p1, "text": "Retry flaky tests once."}).json()["item"]
    assert au["status"] == "proposed"
    s = client.post(f"/v1/{ns}/items/{au['item_id']}:support", json={"project": p1, "run_id": "r9"}).json()
    assert s["auto_active"] is True and s["item"]["status"] == "active"
    assert s["item"]["status_reason"] == "auto_after_evidence"


def test_expire_reindex_search_and_type_usage(client, monkeypatch):
    ns, (p,) = _env(client)
    it = _propose(client, ns, p, "Rotate the signing key every quarter.", ttl_days=1).json()["item"]
    client.patch(f"/v1/{ns}/items/{it['item_id']}", json={"status": "active"})
    hits = items.search(ns, "Rotate the signing key every quarter.", 3)
    assert hits and hits[0]["item_id"] == it["item_id"] and hits[0]["score"] > 0.9
    with sqldb.connect() as c:
        c.execute("UPDATE kh_item SET expires_ms=1 WHERE ns=? AND item_id=?", (ns, it["item_id"]))
    ag = _tok(client, ns, "ag2", "agent")
    enable_signin(monkeypatch)
    assert client.post(f"/v1/{ns}/items:expire", json={}, headers=ag).status_code == 403
    monkeypatch.delenv("KH_AUTH_ISSUER")
    from app import config
    monkeypatch.setattr(config, "AUTH_ISSUER", None)
    ex = client.post(f"/v1/{ns}/items:expire", json={}).json()
    assert ex == {"expired": 1, "item_ids": [it["item_id"]]}
    got = client.get(f"/v1/{ns}/items/{it['item_id']}").json()
    assert got["status"] == "withdrawn" and got["status_reason"] == "expired"
    assert items.search(ns, "Rotate the signing key every quarter.", 3) == []
    with sqldb.connect() as c:
        c.execute("UPDATE kh_item SET vec_rev=0 WHERE ns=?", (ns,))
    assert client.post(f"/v1/{ns}/items:reindex", json={}).json() == {"reindexed": 1}
    assert client.post(f"/v1/{ns}/items:reindex", json={"all": True}).json() == {"reindexed": 1}
    # a manifest that drops the used type -> 409 type_in_use
    m = {k: v for k, v in ITEM_MANIFEST.items()}
    m["types"] = [t for t in ITEM_MANIFEST["types"] if t["type"] != "lesson"]
    etag = client.get(f"/v1/{ns}/manifest").headers.get("ETag") or "*"
    r = client.put(f"/v1/{ns}/manifest", json=m, headers={"If-Match": etag})
    assert r.status_code == 409 and r.json()["error"] == "type_in_use"


def test_project_visibility(client, monkeypatch):
    ns, (p1, p2) = _env(client, 2)
    a = _propose(client, ns, p1, "Use the staging bucket for tests.").json()["item"]
    r = client.post(f"/v1/{ns}/principals", json={"role": "agent", "principal_id": "only2", "projects": [p2]})
    assert r.status_code == 201, r.text
    h = {"Authorization": f"Bearer {client.post(f'/v1/{ns}/principals/only2/token').json()['access_token']}"}
    enable_signin(monkeypatch)
    assert client.get(f"/v1/{ns}/items/{a['item_id']}", headers=h).status_code == 404
    assert client.get(f"/v1/{ns}/items", headers=h).json()["items"] == []
    assert client.get(f"/v1/{ns}/items").status_code == 401
