"""Promote: personal rules -> common tier (§4.5)."""
from __future__ import annotations

import hashlib
import json

import pytest

from app import config, distill, promote, sqldb
from app import vector_store as vs
from app.embeddings import get_embedder
from tests.learning.conftest import ACTION, iso


def _put(coll_name, rid, text, **meta):
    h = vs.get_or_create(coll_name)
    vs.upsert_one(h, rid, get_embedder().encode(text, normalize_embeddings=True), text,
                  {"ts": iso(), "status": "active", **meta})


@pytest.fixture
def promo_on(monkeypatch):
    monkeypatch.setenv("KH_PROMOTE", "on")


@pytest.fixture
def rows(fresh_store, client, uid):
    """Two people asking for the same rule, one project feedback row, and rows that must be skipped."""
    pa, pb = f"personal-a-{uid}-x-com", f"personal-b-{uid}-x-com"
    pre = vs.scope_prefix(config.PERSONAL_SCOPE)
    _put(pre + pa, f"pa_{uid}", "Always read the error log before changing code.", scope=config.PERSONAL_SCOPE,
         project=pa)
    _put(pre + pb, f"pb_{uid}", "Read the error log first, then change code.", scope=config.PERSONAL_SCOPE,
         project=pb)
    _put(pre + pb, f"pg_{uid}", "guard row", scope=config.PERSONAL_SCOPE, project=pb, guard_tool="Bash")
    _put(pre + pb, f"pw_{uid}", "withdrawn row", scope=config.PERSONAL_SCOPE, project=pb, status="withdrawn")
    proj = f"fb-{uid}"
    _put(vs.part_collection_name("knowledge", proj), f"fbk_{uid}", "Check logs before you touch anything.",
         kind="feedback", project=proj)
    _put(vs.part_collection_name("knowledge", proj), f"nfb_{uid}", "not feedback", kind="lesson", project=proj)
    return {"pa": pa, "pb": pb, "proj": proj, "ids": [f"pa_{uid}", f"pb_{uid}", f"fbk_{uid}"]}


def test_off_by_default(client, monkeypatch):
    monkeypatch.delenv("KH_PROMOTE", raising=False)
    for r in (client.get("/promote/next"), client.post("/promote/submit", json={"items": []})):
        assert r.status_code == 403 and r.json()["detail"] == "promote is off"


def test_next_payload(promo_on, rows, client):
    r = client.get("/promote/next")
    assert r.status_code == 200
    j = r.json()
    assert set(j) == {"rows", "common", "kernel", "proposals", "rules"}
    assert j["rules"] == {"min_people": 2, "text_max": 600, "tier": "common"}
    by = {x["id"]: x for x in j["rows"]}
    assert set(by) == set(rows["ids"])
    a = by[rows["ids"][0]]
    assert a["owner"] == "person-" + hashlib.sha256(rows["pa"].encode()).hexdigest()[:8]
    assert a["layer"] == "personal" and set(a) == {"id", "owner", "layer", "text", "ts"}
    f = by[rows["ids"][2]]
    assert f["owner"] == f"project:{rows['proj']}" and f["layer"] == "project"
    acts = {x["action"]: x for x in j["common"]}
    assert ACTION in acts and acts[ACTION]["description"]
    assert {"sub_action", "seq", "content"} == set(acts[ACTION]["steps"][0])
    assert isinstance(j["kernel"], str) and j["proposals"] == []
    assert promote.min_people() == 2 and promote.max_rows() == 400


def test_submit_checks_store_merge_and_approve(promo_on, rows, client):
    ida, idb, idf = rows["ids"]
    steps = {s["sub_action"]: s["seq"] for s in
             {a["action"]: a for a in distill.common_actions()}[ACTION]["steps"]}
    sub = next(iter(steps))
    good = {"title": "Read the error log first", "text": "Read the error log before changing any code.",
            "target_action": ACTION, "target_sub_action": sub, "sources": [ida, idb], "why": "two people"}
    bad = [
        (dict(good, sources=[ida]), "needs at least 2 distinct owners (got 1)"),
        (dict(good, target_action="nope"), "target_action 'nope' is not a common action"),
        (dict(good, sources=[ida, "zzz"]), "sources must all be ids of the current rows"),
        (dict(good, title="t" * 121), "title must be 1..120 characters"),
        (dict(good, text="x" * 601), "text must be 1..600 characters"),
        (dict(good, target_sub_action=" "), "target_sub_action must not be empty"),
        (dict(good, text="mail a@b.com"), "leak gate: 'a@b.com'"),
        (dict(good, text="the orchestrator decides"), "leak gate: 'orchestrator'"),
    ]
    r = client.post("/promote/submit", json={"items": [b for b, _ in bad], "distilled_by": "t"}).json()
    assert [x["reason"] for x in r["results"]] == [reason for _, reason in bad]
    # dry run: verdict only
    r = client.post("/promote/submit", json={"items": [good], "distilled_by": "t", "dry_run": True}).json()
    assert r["dry_run"] is True and r["results"][0]["result"] == "accepted"
    assert promote.proposals() == []
    r = client.post("/promote/submit", json={"items": [good], "distilled_by": "t"}).json()
    res = r["results"][0]
    key = distill.dkey(config.ORCHESTRATOR_ID, "policy_proposal", ACTION, sub, "common")
    assert res == {"n": 0, "result": "accepted", "id": key}
    it = distill.get_item(config.ORCHESTRATOR_ID, key)
    m = it["metadata"]
    assert m["kind"] == "policy_proposal" and m["status"] == "proposed" and m["tier"] == "common"
    assert m["people"] == 2 and m["seq"] == steps[sub] and m["target_action"] == ACTION
    assert m["target_sub_action"] == sub and json.loads(m["sources_json"]) == [ida, idb]
    # same text again: merged, sources merged
    r = client.post("/promote/submit", json={"items": [dict(good, sources=[ida, idf])]}).json()
    assert r["results"][0] == {"n": 0, "result": "merged", "id": key}
    assert distill.get_item(config.ORCHESTRATOR_ID, key)["sources"] == [ida, idb, idf]
    props = client.get("/promote/next").json()["proposals"]
    assert [p["id"] for p in props] == [key]
    # approval writes the step into the every-project tier, the proposal becomes superseded
    with sqldb.connect() as c:
        old = c.execute("SELECT content FROM policy_rules WHERE project=? AND action=? AND sub_action=?",
                        ("every project", ACTION, sub)).fetchone()[0]
    try:
        ap = client.post("/distill/approve", json={"project": config.ORCHESTRATOR_ID, "ids": [key]}).json()
        assert ap["results"][0] == {"id": key, "result": "applied", "applied_to": f"every project:{ACTION}/{sub}"}
        with sqldb.connect() as c:
            row = c.execute("SELECT content, seq FROM policy_rules WHERE project=? AND action=? AND sub_action=?",
                            ("every project", ACTION, sub)).fetchone()
        assert row[0] == good["text"] and row[1] == steps[sub]
        assert distill.get_item(config.ORCHESTRATOR_ID, key)["status"] == "superseded"
    finally:
        with sqldb.connect() as c:
            c.execute("UPDATE policy_rules SET content=? WHERE project=? AND action=? AND sub_action=?",
                      (old, "every project", ACTION, sub))


def test_new_step_gets_seq_99(promo_on, rows, client):
    ida, idb, _ = rows["ids"]
    r = client.post("/promote/submit", json={"items": [{
        "title": "Brand new step", "text": "Write the reproduction steps down first.", "target_action": ACTION,
        "target_sub_action": "write-repro", "sources": [ida, idb]}]}).json()
    iid = r["results"][0]["id"]
    assert distill.get_item(config.ORCHESTRATOR_ID, iid)["metadata"]["seq"] == 99


def test_worker_dry_forced(promo_on, rows, monkeypatch):
    class W:
        ns, svc_role, principal_id, email, is_maintainer = "kh_people", "distiller", "worker", None, False

    monkeypatch.setenv("KH_DISTILL_WORKER", "dry")
    ida, idb, _ = rows["ids"]
    out = promote.submit({"items": [{"title": "T", "text": "Read the error log first.", "target_action": ACTION,
                                     "target_sub_action": "x", "sources": [ida, idb]}]}, caller=W())
    assert out["dry_run"] is True and out["forced_dry"] is True


def test_member_refused(promo_on, client, monkeypatch):
    from tests.auth_helpers import bearer, enable_signin, make_token

    enable_signin(monkeypatch)
    assert client.get("/promote/next", headers=bearer(make_token("m@x.com"))).status_code == 403
