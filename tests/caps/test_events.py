"""§6.4 events: append, gapless seq, cursor reads, long poll, stats, ACLs, fencing."""
from __future__ import annotations

import time

from tests.caps.conftest import attach


def _post(client, ns, payload, **kw):
    return client.post(f"/v1/{ns}/events", json=payload, **kw)


def test_append_and_read(ns_env):
    client, ns, (project,) = ns_env
    r = _post(client, ns, {"events": [
        {"kind": "step.done", "body": {"n": 1, "who": "a"}},
        {"id": "my-id-1", "kind": "step.done", "project": project, "ts_ms": 1000, "body": {"n": 2, "who": "b"}},
    ]})
    assert r.status_code == 200
    acc = r.json()["accepted"]
    assert [a["index"] for a in acc] == [0, 1] and acc[1]["id"] == "my-id-1"
    assert acc[1]["seq"] == acc[0]["seq"] + 1 and not acc[0]["duplicate"]
    assert r.json()["rejected"] == []
    assert len(acc[0]["id"]) == 24
    # duplicate id keeps original seq
    d = _post(client, ns, {"id": "my-id-1", "kind": "step.done", "body": {"n": 2}})
    assert d.json()["accepted"][0] == {"index": 0, "id": "my-id-1", "seq": acc[1]["seq"], "duplicate": True}
    got = client.get(f"/v1/{ns}/events", params={"kind": "step.done"}).json()
    assert [e["seq"] for e in got["events"]] == [acc[0]["seq"], acc[1]["seq"]]
    assert got["next_after_seq"] == acc[1]["seq"]
    e = got["events"][1]
    assert e["project"] == project and e["ts_ms"] == 1000 and e["ix_s1"] == "b" and e["ix_n1"] == 2.0
    assert e["body"] == {"n": 2, "who": "b"} and e["received_ms"] > 0 and e["schema_ver"] == 1
    after = client.get(f"/v1/{ns}/events", params={"after_seq": acc[0]["seq"], "kind": "step.done"}).json()
    assert [x["id"] for x in after["events"]] == ["my-id-1"]
    one = client.get(f"/v1/{ns}/events/my-id-1")
    assert one.status_code == 200 and one.json()["seq"] == acc[1]["seq"]
    assert client.get(f"/v1/{ns}/events/nope").status_code == 404
    lim = client.get(f"/v1/{ns}/events", params={"limit": 1}).json()["events"]
    assert len(lim) == 1


def test_gapless_seq(ns_env):
    client, ns, _ = ns_env
    for i in range(5):
        _post(client, ns, {"events": [{"kind": "step.done", "body": {"n": i}} for _ in range(3)]})
    seqs = [e["seq"] for e in client.get(f"/v1/{ns}/events", params={"limit": 1000}).json()["events"]]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))


def test_validation(ns_env):
    client, ns, _ = ns_env
    bad = {"events": [{"kind": "step.done", "body": {"n": "x"}}, {"kind": "nope", "body": {}},
                      {"kind": "kh.duplicate", "body": {}}, {"kind": "step.done", "id": "bad id!", "body": {"n": 1}},
                      {"kind": "run.start", "body": {}}, {"kind": "step.done", "body": {"n": 5}}]}
    r = _post(client, ns, bad)
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "validation_failed" and body["accepted"] == []
    assert [x["index"] for x in body["rejected"]] == [0, 1, 2, 3, 4]
    assert body["rejected"][0]["errors"][0]["pointer"] == "/body/n"
    p = client.post(f"/v1/{ns}/events?partial=true", json=bad)
    assert p.status_code == 200 and [a["index"] for a in p.json()["accepted"]] == [5]
    assert len(p.json()["rejected"]) == 5
    ok = _post(client, ns, {"kind": "run.start", "body": {}}, headers={"X-KH-Run": "r9"})
    assert ok.status_code == 200
    big = _post(client, ns, {"kind": "step.done", "body": {"n": 1, "who": "x" * (256 * 1024)}})
    assert big.status_code == 422
    many = _post(client, ns, {"events": [{"kind": "step.done", "body": {"n": 1}}] * 501})
    assert many.status_code == 413 and many.json()["error"] == "too_large"
    wrong_project = _post(client, ns, {"kind": "step.done", "project": "elsewhere", "body": {"n": 1}})
    assert wrong_project.status_code == 422


def test_fenced_kind(ns_env):
    client, ns, _ = ns_env
    r = _post(client, ns, {"kind": "unit.write", "body": {}})
    assert r.status_code == 428 and r.json()["error"] == "precondition_required"
    lease = client.post(f"/v1/{ns}/leases/unit/7:acquire", json={"ttl_s": 30}).json()
    h = {"X-KH-Lease": "unit/7", "X-KH-Epoch": str(lease["epoch"])}
    ok = _post(client, ns, {"kind": "unit.write", "body": {}}, headers=h)
    assert ok.status_code == 200
    ev = client.get(f"/v1/{ns}/events/{ok.json()['accepted'][0]['id']}").json()
    assert ev["epoch"] == lease["epoch"]
    stale = _post(client, ns, {"kind": "unit.write", "body": {}},
                  headers={"X-KH-Lease": "unit/7", "X-KH-Epoch": str(lease["epoch"] + 5)})
    assert stale.status_code == 409
    b = stale.json()
    assert b["error"] == "stale_epoch" and b["current_epoch"] == lease["epoch"] and b["lease"] == "unit/7"
    kinds = [e["kind"] for e in client.get(f"/v1/{ns}/events", params={"kind": "kh.stale_epoch"}).json()["events"]]
    assert kinds == ["kh.stale_epoch"]


def test_long_poll(ns_env):
    client, ns, _ = ns_env
    last = client.get(f"/v1/{ns}/events", params={"limit": 1000}).json()["next_after_seq"]
    t = time.monotonic()
    r = client.get(f"/v1/{ns}/events", params={"after_seq": last, "wait_s": 0.6}).json()
    assert r["events"] == [] and r["next_after_seq"] == last and time.monotonic() - t >= 0.5


def test_stats(ns_env):
    client, ns, _ = ns_env
    _post(client, ns, {"events": [{"kind": "step.done", "run_id": "r1", "body": {"n": 1, "who": "a"}},
                                  {"kind": "step.done", "run_id": "r1", "body": {"n": 1, "who": "b"}},
                                  {"kind": "step.done", "run_id": "r2", "body": {"n": 1, "who": "a"}}]})
    r = client.get(f"/v1/{ns}/events:stats", params={"group_by": "kind,run_id"}).json()
    groups = {(g["kind"], g["run_id"]): g["n"] for g in r["groups"]}
    assert groups[("step.done", "r1")] == 2 and groups[("step.done", "r2")] == 1
    r = client.get(f"/v1/{ns}/events:stats", params={"group_by": "ix_s1", "since": "0"}).json()
    assert {g["ix_s1"]: g["n"] for g in r["groups"] if g["ix_s1"]} == {"a": 2, "b": 1}
    assert client.get(f"/v1/{ns}/events:stats", params={"group_by": "body"}).status_code == 422
    assert client.get(f"/v1/{ns}/events:stats",
                      params={"group_by": "kind,project,run_id,unit_id,session_id"}).status_code == 422


def test_role_acl_hides_kinds(client):
    from tests.caps.conftest import EVENT_MANIFEST

    ns, _, _ = attach(client, manifest=EVENT_MANIFEST)
    assert _post(client, ns, {"kind": "secret.ev", "body": {}}).status_code == 200
    from app.caps import core, events

    st = core.ns_state(ns, fresh=True)
    grader = core.Ctx(ns=ns, st=st, caller=core.Caller("g1", "grader", "service", None, ["*"]))
    judge = core.Ctx(ns=ns, st=st, caller=core.Caller("j1", "judge", "service", None, ["*"]))
    assert "secret.ev" in events.hidden_kinds(grader)
    assert "secret.ev" not in events.hidden_kinds(judge)
    assert all(e["kind"] != "secret.ev" for e in events.read(grader, 0, [], None, None, 1000))
    assert any(e["kind"] == "secret.ev" for e in events.read(judge, 0, [], None, None, 1000))
