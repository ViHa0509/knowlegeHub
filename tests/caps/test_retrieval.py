"""§6.10 retrieval: namespace /context/step tiers, people lanes, worklog, projects:match."""
from __future__ import annotations

import uuid

from app.caps import retrieval
from tests.caps.conftest import attach, new_project
from tests.caps.test_items import ITEM_MANIFEST


def _env(client, manifest=None, n=1):
    projects = [new_project() for _ in range(n)]
    ns, _, _ = attach(client, projects=projects, manifest=manifest or ITEM_MANIFEST)
    return ns, projects


def _item(client, ns, project, text, key, activate=True, **kw):
    r = client.post(f"/v1/{ns}/items", json={"kind": "lesson", "keyspace": "task", "key": key, "project": project,
                                             "text": text, **kw})
    assert r.status_code == 201, r.text
    it = r.json()["item"]
    if activate:
        assert client.patch(f"/v1/{ns}/items/{it['item_id']}", json={"status": "active"}).status_code == 200
    return it


def _events(client, ns, kind):
    out = client.get(f"/v1/{ns}/events", params={"kinds": kind}).json()
    return [e for e in (out.get("events") or out.get("items") or []) if e["kind"] == kind]


def test_step_key_tiers_and_served_event(client):
    ns, (p,) = _env(client)
    exact = _item(client, ns, p, "Run the unit tests before packaging.", {"area": "build", "step": "pack"},
                  title="Test first")
    wild = _item(client, ns, p, "Never package from a dirty tree.", {"area": "build"})
    prop = _item(client, ns, p, "Sign the package after building.", {"area": "build", "step": "pack"}, activate=False)
    trig = _item(client, ns, p, "Bump the version on release builds.", {"area": "build", "step": "pack"},
                 triggers=["release"])
    r = client.post(f"/v1/{ns}/context/step", json={"project": p, "keyspace": "task",
                                                    "key": {"area": "build", "step": "pack"}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["serve_id"].startswith("sv_") and len(out["serve_id"]) == 27
    tiers = {s["id"]: s["tier"] for s in out["served"]}
    assert tiers[exact["item_id"]] == "key" and tiers[wild["item_id"]] == "key-wildcard"
    assert tiers[prop["item_id"]] == "key" and trig["item_id"] not in tiers
    assert out["item_ids"][0] == exact["item_id"]  # active before proposed
    lines = out["pack_text"].split("\n")
    assert lines[0] == f"{exact['item_id']} lesson [build/pack] Test first: Run the unit tests before packaging."
    assert any("[unverified]" in ln and prop["item_id"] in ln for ln in lines)
    assert out["people_lanes"] is True and out["budget_chars"] == 6000 and out["cut_at_budget"] is False
    ev = _events(client, ns, "kh.served")
    assert ev and ev[-1]["body"]["serve_id"] == out["serve_id"] and ev[-1]["body"]["item_ids"] == out["item_ids"]
    # situation unlocks the trigger item; serve_proposed false hides the proposed one
    r2 = client.post(f"/v1/{ns}/context/step", json={"project": p, "keyspace": "task", "situation": "release",
                                                     "key": "area=build;step=pack", "tiers": ["key"]}).json()
    assert trig["item_id"] in r2["item_ids"] and r2["tiers"] == ["key"]


def test_step_validation_budget_and_meaning(client):
    ns, (p,) = _env(client)
    for i in range(6):
        _item(client, ns, p, f"Check the deployment manifest number {i} " + "carefully " * 10,
              {"area": "deploy", "step": f"s{i}"})
    bad = client.post(f"/v1/{ns}/context/step", json={"project": p, "keyspace": "nope", "key": {}})
    assert bad.status_code == 422
    assert client.post(f"/v1/{ns}/context/step", json={"project": p, "keyspace": "task", "key": {},
                                                       "budget_chars": 100}).status_code == 422
    assert client.post(f"/v1/{ns}/context/step", json={"project": p, "keyspace": "task", "key": {},
                                                       "tiers": ["bogus"]}).status_code == 422
    r = client.post(f"/v1/{ns}/context/step", json={
        "project": p, "keyspace": "task", "key": {"area": "other"}, "tiers": ["meaning"], "budget_chars": 800,
        "query": "Check the deployment manifest number 3 " + "carefully " * 10}).json()
    assert r["served"] and r["served"][0]["tier"] == "meaning"
    assert r["used_chars"] <= 800 and r["cut_at_budget"] is True
    assert all(d["reason"] == "budget" for d in r["dropped"])


def test_people_lanes_and_cross_share(client):
    ns, (p,) = _env(client)
    long_log = "Deployed the payment gateway to staging and checked the payment webhooks. " * 20
    w = client.post(f"/v1/{ns}/worklog", json={"project": p, "text": long_log, "subject": "deploy"})
    assert w.status_code == 201, w.text
    q = {"project": p, "keyspace": "task", "key": {}, "tiers": ["log"], "query": long_log, "budget_chars": 1000}
    r = client.post(f"/v1/{ns}/context/step", json=q).json()
    assert r["people_lanes"] is True
    assert [d["reason"] for d in r["dropped"]] == ["cross_share"]  # 600-char row > 40% of 1000
    r = client.post(f"/v1/{ns}/context/step", json={**q, "budget_chars": 10000}).json()
    assert [s["tier"] for s in r["served"]] == ["log"] and r["served"][0]["id"] == w.json()["id"]
    off = {**ITEM_MANIFEST, "settings": {"retrieval": {"include_people_lanes": False}}}
    ns2, (p2,) = _env(client, off)
    r = client.post(f"/v1/{ns2}/context/step", json={**q, "project": p2, "budget_chars": 10000}).json()
    assert r["people_lanes"] is False and r["served"] == []


def test_failed_tier_and_provider(client, monkeypatch):
    ns, (p,) = _env(client)

    def boom(ctx, req):
        raise RuntimeError("down")

    monkeypatch.setitem(retrieval.PROVIDERS, "packs", boom)
    r = client.post(f"/v1/{ns}/context/step", json={"project": p, "keyspace": "task", "key": {}}).json()
    assert r["failed_tiers"] == [{"tier": "packs", "error": "down"}]
    monkeypatch.setitem(retrieval.PROVIDERS, "packs", lambda ctx, req: [{"id": "pk1", "text": "Quote from pack."}])
    r = client.post(f"/v1/{ns}/context/step", json={"project": p, "keyspace": "task", "key": {}}).json()
    assert {"tier": "packs", "id": "pk1", "chars": 16} in r["served"] and "failed_tiers" not in r


def test_worklog(client, monkeypatch):
    ns, (p,) = _env(client)
    body = {"project": p, "text": "Fixed the flaky login test; contact dev@example.com."}
    a = client.post(f"/v1/{ns}/worklog", json=body).json()
    b = client.post(f"/v1/{ns}/worklog", json=body).json()
    assert a["id"] == b["id"] and a["id"].startswith("vec_") and a["subject"] == "agent worklog"
    from app import vector_store as vs
    from app.hotlog import collection, enabled
    from app import config

    coll = collection() if enabled() else vs.get_or_create(vs.part_collection_name(config.HOTLOG_SCOPE, p))
    row = vs.get_ids_vt(coll, [a["id"]])[0]
    assert "[REDACTED:email]" in row["source_text"]
    m = row["metadata"]
    assert m["author"] == f"ns:{ns}/local" and m["topic"] == m["subject"] == "agent worklog" and m["project"] == p
    assert client.post(f"/v1/{ns}/worklog", json={"project": p, "text": ""}).status_code == 422
    assert client.post(f"/v1/{ns}/worklog", json={"project": p, "text": "x", "subject": "s" * 129}).status_code == 422
    from app.embeddings import EmbedderUnavailable
    import app.embeddings as E

    def nope():
        raise EmbedderUnavailable("no model")

    monkeypatch.setattr(E, "get_embedder", nope)
    r = client.post(f"/v1/{ns}/worklog", json={"project": p, "text": "other text"})
    assert r.status_code == 503 and r.json()["error"] == "embedder_unavailable"


def test_projects_match(client):
    from app import projects

    pid = "zq" + uuid.uuid4().hex[:6]
    projects.get_or_create_project(pid, aliases=["zebra-quokka"], descriptor="the zebra quokka service")
    other = "zq" + uuid.uuid4().hex[:6]
    projects.get_or_create_project(other)
    projects.sync_orchestrator_registry()
    ns, _, _ = attach(client, projects=[pid], manifest=ITEM_MANIFEST)
    r = client.post(f"/v1/{ns}/projects:match", json={"text": f"work on zebra-quokka and {other} today"}).json()
    assert r["best"] == pid and [m["project"] for m in r["matches"]] == [pid]
    assert r["matches"][0]["descriptor"] == "the zebra quokka service"
    assert "zebra-quokka" in r["matches"][0]["aliases"]
    assert client.post(f"/v1/{ns}/projects:match", json={}).status_code == 422


def test_people_items_reverse(client):
    ns, (p,) = _env(client)
    it = _item(client, ns, p, "Warm the cache before the load test.", {"area": "perf"}, triggers=["perf:load"])
    rows = retrieval.people_items(p, "", "run:perf:load")
    assert rows == [] or rows[0]["id"] == it["item_id"]
    rows = retrieval.people_items(p, "", "perf:load")
    assert rows and rows[0]["id"] == it["item_id"] and rows[0]["ns"] == ns
