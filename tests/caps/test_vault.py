"""§6.11 vault: declared vaults, grader-only access, items, canaries, leak detection, refresh."""
from __future__ import annotations

from app import sqldb
from app.caps import vault
from tests.caps.conftest import EVENT_MANIFEST, attach

MANIFEST = {**EVENT_MANIFEST, "vaults": [{"vault_id": "holdout", "refresh_days": 30, "retire_days": 10},
                                         {"vault_id": "locked", "fenced": True}]}


def _env(client):
    ns, projects, _ = attach(client, manifest=MANIFEST)
    return ns, projects[0]


def _tok(client, ns, pid, role):
    r = client.post(f"/v1/{ns}/principals", json={"role": role, "principal_id": pid})
    assert r.status_code in (200, 201), r.text
    t = client.post(f"/v1/{ns}/principals/{pid}/token").json()["access_token"]
    return {"Authorization": f"Bearer {t}"}


def _events(client, ns, kind):
    return [e for e in client.get(f"/v1/{ns}/events", params={"limit": 1000}).json()["events"] if e["kind"] == kind]


def test_declared_and_items(client):
    ns, project = _env(client)
    st = client.get(f"/v1/{ns}/vault").json()["vaults"]
    assert {v["vault_id"] for v in st} == {"holdout", "locked"}
    assert client.get(f"/v1/{ns}/vault/nope").json()["error"] == "vault_not_declared"
    assert client.get(f"/v1/{ns}/vault/nope").status_code == 404

    p = client.put(f"/v1/{ns}/vault/holdout/items/q1",
                   json={"body": {"q": "what is 2+2", "a": "4"}, "canaries": ["purple-elephant-77"], "kh_canaries": 2})
    assert p.status_code == 201, p.text
    item = p.json()
    assert "body" not in item and item["version"] == 1 and item["kh_canaries"] == 2 and item["custom_canaries"] == 1
    assert item["project"] == "" and p.headers["ETag"] == '"1"'
    kh = [c for c in item["canaries"] if c.startswith("khc-")]
    assert len(kh) == 2 and all(len(c) == 20 for c in kh)

    same = client.put(f"/v1/{ns}/vault/holdout/items/q1", json={"body": {"a": "4", "q": "what is 2+2"}})
    assert same.status_code == 200 and same.json()["version"] == 1 and same.json()["rev"] == 2
    changed = client.put(f"/v1/{ns}/vault/holdout/items/q1", json={"body": {"q": "x", "a": "y"}},
                         headers={"If-Match": '"2"'})
    assert changed.json()["version"] == 2 and changed.json()["rev"] == 3
    stale = client.put(f"/v1/{ns}/vault/holdout/items/q1", json={"body": {}}, headers={"If-Match": '"1"'})
    assert stale.status_code == 409 and stale.json()["error"] == "write_conflict"
    inm = client.put(f"/v1/{ns}/vault/holdout/items/q1", json={"body": {}}, headers={"If-None-Match": "*"})
    assert inm.status_code == 409

    g = client.get(f"/v1/{ns}/vault/holdout/items/q1")
    assert g.status_code == 200 and g.json()["body"] == {"q": "x", "a": "y"}
    assert set(g.json()["canaries"]) == set(item["canaries"])
    client.put(f"/v1/{ns}/vault/holdout/items/q2", json={"body": "two"})
    lst = client.get(f"/v1/{ns}/vault/holdout/items", params={"limit": 1}).json()
    assert [i["item_id"] for i in lst["items"]] == ["q1"] and lst["next"] == "q1"
    lst2 = client.get(f"/v1/{ns}/vault/holdout/items", params={"after": "q1"}).json()
    assert [i["item_id"] for i in lst2["items"]] == ["q2"] and "next" not in lst2
    by_ids = client.get(f"/v1/{ns}/vault/holdout/items", params={"ids": "q2,q1,zz"}).json()
    assert [i["item_id"] for i in by_ids["items"]] == ["q1", "q2"]
    reads = _events(client, ns, "kh.vault_read")
    assert reads and reads[-1]["body"]["item_ids"] == ["q1", "q2"] and "body" not in reads[-1]["body"]

    st = client.get(f"/v1/{ns}/vault/holdout").json()
    assert st["items"] == 2 and st["canaries"]["active_kh"] == 3 and st["canaries"]["active_custom"] == 1
    assert st["refresh_due"] is False and st["next_due_ms"] > st["created_ms"]

    # custom canaries: unique per ns, not KH format, 12..256 chars
    conflict = client.put(f"/v1/{ns}/vault/holdout/items/q3", json={"body": 1, "canaries": ["purple-elephant-77"]})
    assert conflict.status_code == 409 and conflict.json()["error"] == "canary_conflict"
    assert client.put(f"/v1/{ns}/vault/holdout/items/q3", json={"body": 1, "canaries": ["short"]}).status_code == 422
    assert client.put(f"/v1/{ns}/vault/holdout/items/q3",
                      json={"body": 1, "canaries": ["khc-0123456789abcdef"]}).status_code == 422
    assert client.put(f"/v1/{ns}/vault/holdout/items/q3", json={"body": 1, "kh_canaries": 6}).status_code == 422
    assert client.put(f"/v1/{ns}/vault/holdout/items/bad id", json={"body": 1}).status_code == 422
    assert client.put(f"/v1/{ns}/vault/holdout/items/q3", json={}).status_code == 422

    d = client.delete(f"/v1/{ns}/vault/holdout/items/q2")
    assert d.status_code == 200 and d.json()["deleted"] == "q2" and d.json()["retired_canaries"] == 1
    assert client.get(f"/v1/{ns}/vault/holdout/items/q2").status_code == 404
    assert client.delete(f"/v1/{ns}/vault/holdout/items/q2").status_code == 404


def test_fenced_vault(client):
    ns, _ = _env(client)
    r = client.put(f"/v1/{ns}/vault/locked/items/a", json={"body": 1})
    assert r.status_code == 428
    a = client.post(f"/v1/{ns}/leases/vw:acquire", json={}).json()
    ok = client.put(f"/v1/{ns}/vault/locked/items/a", json={"body": 1},
                    headers={"X-KH-Lease": "vw", "X-KH-Epoch": str(a["epoch"])})
    assert ok.status_code == 201
    stale = client.put(f"/v1/{ns}/vault/locked/items/a", json={"body": 2},
                       headers={"X-KH-Lease": "vw", "X-KH-Epoch": "99"})
    assert stale.status_code == 409 and stale.json()["error"] == "stale_epoch"


def test_canary_leak_is_masked_and_flagged(client):
    ns, _ = _env(client)
    item = client.put(f"/v1/{ns}/vault/holdout/items/q1",
                      json={"body": "secret question", "canaries": ["purple-elephant-77"]}).json()
    kh = next(c for c in item["canaries"] if c.startswith("khc-"))
    r = client.post(f"/v1/{ns}/events", json={"events": [
        {"kind": "step.done", "body": {"n": 1, "who": f"I saw {kh} and purple-elephant-77 here"}}]})
    assert r.status_code == 200, r.text
    ev = [e for e in client.get(f"/v1/{ns}/events", params={"kind": "step.done"}).json()["events"]][-1]
    assert kh not in ev["body"]["who"] and "purple-elephant-77" not in ev["body"]["who"]
    assert ev["body"]["who"].count("[REDACTED:canary]") == 2
    hits = _events(client, ns, "kh.canary_hit")
    assert len(hits) == 2
    b = hits[0]["body"]
    assert b["vault"] == "holdout" and b["item"] == "q1" and b["retired"] is False
    assert {h["body"]["canary_kind"] for h in hits} == {"kh", "custom"}
    assert kh not in str(hits) and "purple-elephant-77" not in str(hits)
    # an unknown KH-format string is not a canary
    assert vault.scan(ns, "khc-0000000000000000") == []
    # vault bodies themselves are not canary-scanned
    again = client.put(f"/v1/{ns}/vault/holdout/items/q9", json={"body": f"mentions {kh}"})
    assert again.status_code == 201
    assert client.get(f"/v1/{ns}/vault/holdout/items/q9").json()["body"] == f"mentions {kh}"


def test_refresh_rotates_and_retired_still_detected(client):
    ns, _ = _env(client)
    item = client.put(f"/v1/{ns}/vault/holdout/items/q1", json={"body": 1, "kh_canaries": 2}).json()
    old = [c for c in item["canaries"] if c.startswith("khc-")]
    st = client.get(f"/v1/{ns}/vault/holdout")
    bad = client.post(f"/v1/{ns}/vault/holdout:refresh", headers={"If-Match": '"999"'})
    assert bad.status_code == 409
    r = client.post(f"/v1/{ns}/vault/holdout:refresh", headers={"If-Match": st.headers["ETag"]})
    assert r.status_code == 200, r.text
    assert r.json()["refresh_count"] == 1 and r.json()["rotated_items"] == 1 and r.json()["canaries"]["retired"] == 2
    new = client.get(f"/v1/{ns}/vault/holdout/items/q1").json()
    assert len(new["canaries"]) == 2 and not set(new["canaries"]) & set(old) and new["refreshed_ms"]
    hits = vault.scan(ns, f"leak {old[0]}")
    assert len(hits) == 1 and hits[0]["retired"] is True
    assert client.post(f"/v1/{ns}/vault/holdout:fly").status_code == 405
    assert [e["body"]["reason"] for e in _events(client, ns, "vault.refresh_due")] == ["manual"]


def test_cron_refresh_and_expiry(client):
    ns, _ = _env(client)
    item = client.put(f"/v1/{ns}/vault/holdout/items/q1", json={"body": 1}).json()
    old = item["canaries"][0]
    with sqldb.connect() as c:
        c.execute("UPDATE kh_vault SET next_due_ms=1 WHERE ns=? AND vault_id='holdout'", (ns,))
    assert vault.refresh_due() >= 1
    st = client.get(f"/v1/{ns}/vault/holdout").json()
    assert st["refresh_count"] == 1 and st["refresh_due"] is False
    ev = _events(client, ns, "vault.refresh_due")
    assert ev and ev[-1]["body"]["reason"] == "schedule" and ev[-1]["body"]["items"] == 1
    with sqldb.connect() as c:
        c.execute("UPDATE kh_vault_canary SET expires_ms=1 WHERE ns=? AND canary=?", (ns, old))
    vault.refresh_due()
    vault.invalidate(ns)
    assert vault.scan(ns, old) == []


def test_roles(client, monkeypatch):
    from tests.auth_helpers import enable_signin

    ns, _ = _env(client)
    grader = _tok(client, ns, "g1", "grader")
    agent = _tok(client, ns, "a1", "agent")
    enable_signin(monkeypatch)
    assert client.get(f"/v1/{ns}/vault", headers=agent).status_code == 403
    assert client.get(f"/v1/{ns}/vault/nope", headers=agent).status_code == 403  # 403 before lookup
    assert client.get(f"/v1/{ns}/vault", headers=grader).status_code == 200
    w = client.put(f"/v1/{ns}/vault/holdout/items/g", json={"body": 1}, headers=grader)
    assert w.status_code == 201
    assert client.get(f"/v1/{ns}/vault/holdout/items/g", headers=agent).status_code == 403
