"""§6.3 coord: leases, effects, approvals, idempotency purge."""
from __future__ import annotations

from app import sqldb
from app.caps import coord, core
from tests.auth_helpers import bearer, enable_signin, make_token
from tests.caps.conftest import attach, new_project


def _tok(client, ns, pid, role):
    client.post(f"/v1/{ns}/principals", json={"role": role, "principal_id": pid})
    return {"Authorization": f"Bearer {client.post(f'/v1/{ns}/principals/{pid}/token').json()['access_token']}"}


def test_lease_lifecycle(client):
    ns, _, _ = attach(client)
    a = client.post(f"/v1/{ns}/leases/build/main:acquire", json={"ttl_s": 30})
    assert a.status_code == 200
    lease = a.json()
    assert lease["name"] == "build/main" and lease["epoch"] == 1 and lease["until_ms"] > 0
    again = client.post(f"/v1/{ns}/leases/build/main:acquire", json={})
    assert again.json()["epoch"] == 1  # same holder keeps the epoch
    rn = client.post(f"/v1/{ns}/leases/build/main:renew", json={"epoch": 1, "ttl_s": 60})
    assert rn.status_code == 200 and rn.json()["epoch"] == 1
    lost = client.post(f"/v1/{ns}/leases/build/main:renew", json={"epoch": 9})
    assert lost.status_code == 409 and lost.json()["error"] == "lease_lost"
    via_header = client.post(f"/v1/{ns}/leases/build/main:renew", json={},
                             headers={"X-KH-Lease": "build/main", "X-KH-Epoch": "1"})
    assert via_header.status_code == 200
    g = client.get(f"/v1/{ns}/leases/build/main").json()
    assert g["held"] is True and g["holder"] == "local" and g["ns"] == ns
    lst = client.get(f"/v1/{ns}/leases", params={"prefix": "build/"}).json()["leases"]
    assert [x["name"] for x in lst] == ["build/main"]
    rel = client.post(f"/v1/{ns}/leases/build/main:release", json={"epoch": 1})
    assert rel.json() == {"name": "build/main", "released": True}
    assert client.get(f"/v1/{ns}/leases/build/main").json()["held"] is False
    assert client.post(f"/v1/{ns}/leases/build/main:acquire", json={}).json()["epoch"] == 1  # same holder
    assert client.get(f"/v1/{ns}/leases/nope").status_code == 404
    assert client.post(f"/v1/{ns}/leases/x:fly", json={}).status_code == 404
    assert client.post(f"/v1/{ns}/leases/x:acquire", json={"ttl_s": 3}).status_code == 422
    assert client.post(f"/v1/{ns}/leases/x:acquire", json={"ttl_s": 301}).status_code == 422
    assert client.post(f"/v1/{ns}/leases/{'n' * 201}:acquire", json={}).status_code == 422


def test_lease_held_by_other(client):
    ns, _, _ = attach(client)
    with sqldb.connect() as c:
        sqldb.lease_acquire(c, ns, "job", "someone", 60_000)
    r = client.post(f"/v1/{ns}/leases/job:acquire", json={})
    assert r.status_code == 409 and r.json()["error"] == "lease_held" and r.json()["holder"] == "someone"
    with sqldb.connect() as c:  # expire it: next acquire takes over with epoch+1
        c.execute("UPDATE kh_lease SET until_ms=1 WHERE ns=? AND name='job'", (ns,))
    r = client.post(f"/v1/{ns}/leases/job:acquire", json={})
    assert r.status_code == 200 and r.json()["epoch"] == 2


def test_effects(client):
    ns, _, _ = attach(client)
    c1 = client.post(f"/v1/{ns}/effects/email/42:claim", json={"ttl_s": 60})
    assert c1.status_code == 201
    e = c1.json()
    assert e["key"] == "email/42" and e["state"] == "claimed" and e["attempts"] == 1 and e["holder"] == "local"
    assert "approval_required" not in e
    with sqldb.connect() as c:  # another holder cannot claim
        c.execute("UPDATE kh_effect SET holder='other' WHERE ns=? AND effect_key='email/42'", (ns,))
    c2 = client.post(f"/v1/{ns}/effects/email/42:claim", json={})
    assert c2.status_code == 409 and c2.json()["error"] == "effect_claimed" and c2.json()["effect"]["holder"] == "other"
    nf = client.post(f"/v1/{ns}/effects/email/42:complete", json={"result": 1})
    assert nf.status_code == 409
    with sqldb.connect() as c:  # claim expired -> re-claim with attempts+1
        c.execute("UPDATE kh_effect SET claimed_until_ms=1 WHERE ns=? AND effect_key='email/42'", (ns,))
    assert client.get(f"/v1/{ns}/effects/email/42").json()["expired"] is True
    c3 = client.post(f"/v1/{ns}/effects/email/42:claim", json={})
    assert c3.status_code == 201 and c3.json()["attempts"] == 2 and c3.json()["holder"] == "local"
    f = client.post(f"/v1/{ns}/effects/email/42:fail", json={"error": "smtp down"})
    assert f.status_code == 200 and f.json()["state"] == "failed" and f.json()["result"] == {"error": "smtp down"}
    c4 = client.post(f"/v1/{ns}/effects/email/42:claim", json={})
    assert c4.status_code == 201 and c4.json()["attempts"] == 3
    done = client.post(f"/v1/{ns}/effects/email/42:complete", json={"result": {"id": "m1"}})
    assert done.json()["state"] == "done" and done.json()["result"] == {"id": "m1"}
    c5 = client.post(f"/v1/{ns}/effects/email/42:claim", json={})
    assert c5.status_code == 409 and c5.json()["effect"]["state"] == "done"
    g = client.get(f"/v1/{ns}/effects/email/42")
    assert g.json()["rev"] >= 5 and g.headers["ETag"] == f'"{g.json()["rev"]}"'
    assert client.get(f"/v1/{ns}/effects/none").status_code == 404
    assert client.post(f"/v1/{ns}/effects/x:claim", json={"ttl_s": 0}).status_code == 422


def test_idempotent_claim(client):
    ns, _, _ = attach(client)
    h = {"Idempotency-Key": "claim-1"}
    a = client.post(f"/v1/{ns}/effects/k1:claim", json={}, headers=h)
    b = client.post(f"/v1/{ns}/effects/k1:claim", json={}, headers=h)
    assert a.status_code == b.status_code == 201 and b.headers["Idempotent-Replay"] == "true"


def test_approval_policy_unit(client):
    ns, _, _ = attach(client, manifest={"settings": {"effects": {"confirm_policy": {"emails": ["Ann@x.com"]}}}})
    st = core.ns_state(ns, fresh=True)
    assert coord.needs_approval(st, "delete/repo") and coord.needs_approval(st, "deploy/prod/web")
    assert not coord.needs_approval(st, "email/1")
    assert coord.approvers(st) == ["ann@x.com"]
    ns2, _, _ = attach(client)
    assert not coord.needs_approval(core.ns_state(ns2, fresh=True), "delete/repo")


def test_approval_flow(monkeypatch, client):
    _ensure_record_tables()
    p = new_project()
    ns, _, _ = attach(client, projects=[p], manifest={"settings": {"effects": {
        "approval_required": ["delete/"], "confirm_policy": {"emails": ["ann@x.com"]}}}})
    agent = _tok(client, ns, "ag1", "agent")
    enable_signin(monkeypatch)
    r = client.post(f"/v1/{ns}/effects/delete/repo:claim", json={}, headers=agent)
    assert r.status_code == 428 and r.json()["error"] == "approval_required"
    assert client.post(f"/v1/{ns}/effects/email/1:claim", json={}, headers=agent).status_code == 201
    # a stranger has no role; the listed approver resolves as confirmer
    stranger = bearer(make_token("bob@x.com", projects={p: "editable"}))
    assert client.post(f"/v1/{ns}/effects/delete/repo:approve", json={"decision": "approve"},
                       headers=stranger).status_code == 403
    ann = bearer(make_token("ann@x.com", projects={p: "readonly"}))
    assert client.post(f"/v1/{ns}/effects/delete/repo:approve", json={"decision": "maybe"},
                       headers=ann).status_code == 422
    rej = client.post(f"/v1/{ns}/effects/delete/repo:approve", json={"decision": "reject"}, headers=ann)
    assert rej.status_code == 200 and rej.json()["approval"]["decision"] == "reject"
    assert client.post(f"/v1/{ns}/effects/delete/repo:claim", json={}, headers=agent).status_code == 428
    ok = client.post(f"/v1/{ns}/effects/delete/repo:approve", json={"decision": "approve", "note": "fine"},
                     headers=ann)
    assert ok.status_code == 200 and ok.json()["approval"]["rev"] == 2
    seen = client.get(f"/v1/{ns}/effects/delete/repo", headers=ann).json()
    assert seen["approval_required"] is True and seen["approval"]["approver"] == "ann@x.com"
    c = client.post(f"/v1/{ns}/effects/delete/repo:claim", json={}, headers=agent)
    assert c.status_code == 201 and c.json()["approval"]["decision"] == "approve"
    # approval is void once the approver is no longer listed
    boss = bearer(make_token("boss@x.com", role="maintainer"))
    m = {"settings": {"effects": {"approval_required": ["delete/"], "confirm_policy": {"emails": ["zed@x.com"]}}}}
    assert client.put(f"/v1/{ns}/manifest", json=m, headers={**boss, "If-Match": "1"}).status_code == 200
    core.invalidate_ns(ns)
    r = client.post(f"/v1/{ns}/effects/delete/other:claim", json={}, headers=agent)
    assert r.status_code == 428
    evs = client.get(f"/v1/{ns}/events", params={"kind": "kh.intervention"}, headers=boss).json()["events"]
    assert len(evs) == 2


def test_idempotency_purge(client):
    ns, _, _ = attach(client)
    with sqldb.connect() as c:
        sqldb.insert_new(c, "kh_idem", {"ns": ns, "principal": "p", "idem_key": "old", "request_sha256": "x",
                                        "state": "done", "created_ms": 1, "updated_ms": 1})
    assert coord.purge_idempotency() >= 1
    with sqldb.connect() as c:
        assert c.execute("SELECT 1 FROM kh_idem WHERE ns=? AND idem_key='old'", (ns,)).fetchone() is None


def _ensure_record_tables():
    """Minimal kh_record / kh_record_rev (the records module is phase 2) so approvals can be stored."""
    with sqldb.connect() as c:
        if sqldb.table_exists(c, "kh_record"):
            return
        c.execute("CREATE TABLE IF NOT EXISTS kh_record (ns TEXT NOT NULL, type TEXT NOT NULL, id TEXT NOT NULL, "
                  "project TEXT, schema_ver INTEGER, rev INTEGER NOT NULL, state TEXT, ix_s1 TEXT, ix_s2 TEXT, "
                  "ix_s3 TEXT, ix_s4 TEXT, ix_n1 REAL, ix_n2 REAL, ix_t1 INTEGER, ix_t2 INTEGER, body_json TEXT, "
                  "actor_json TEXT, created_ms INTEGER, updated_ms INTEGER, PRIMARY KEY (ns, type, id))")
        c.execute("CREATE TABLE IF NOT EXISTS kh_record_rev (ns TEXT NOT NULL, type TEXT NOT NULL, id TEXT NOT NULL, "
                  "rev INTEGER NOT NULL, state TEXT, schema_ver INTEGER, body_json TEXT, actor_json TEXT, "
                  "at_ms INTEGER, PRIMARY KEY (ns, type, id, rev))")
