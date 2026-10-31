"""KH-to-KH sync (§4.11): keys, peer handshake, export, merge, pull jobs, grants, auth."""
from __future__ import annotations

import base64
import json
import re

import pytest

from app import sync
from app import vector_store as vs
from tests.auth_helpers import bearer, enable_signin, make_token
from tests.learning.conftest import iso


@pytest.fixture
def giver(client, uid, add_know, add_log):
    from app import projects

    p = f"sy-{uid}"
    projects.get_or_create_project(p, aliases=[f"alias{uid}"], descriptor="sync source")
    k1 = add_know(p, "Knowledge one about sync.", ts="2026-10-01T00:00:00Z")
    k2 = add_know(p, "Knowledge two about sync.", ts="2026-10-20T00:00:00Z")
    lg = add_log(p, "a sync log line")
    key = client.post("/sync/keys", json={"label": "t", "days": 100, "projects": [p, "personal-a-x-com"],
                                          "public_url": "http://testserver"}).json()
    return {"project": p, "ids": [k1, k2], "log": lg, "key": key}


def test_key_format_and_listing(client, giver):
    k = giver["key"]
    assert re.fullmatch(r"khsync1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", k["key"])
    head = json.loads(base64.urlsafe_b64decode(k["key"].split(".")[1] + "=="))
    assert head == {"u": "http://testserver", "k": k["kid"]}
    assert k["projects"] == [giver["project"]]                       # personal projects are never offered
    lst = {x["kid"]: x for x in client.get("/sync/keys").json()["keys"]}[k["kid"]]
    assert "key" not in lst and "secret_hash" not in lst and lst["revoked_at"] is None
    # days clamped to 1..90
    from datetime import datetime, timedelta, timezone

    exp = datetime.fromisoformat(k["expires_at"].replace("Z", "+00:00"))
    assert abs((exp - datetime.now(timezone.utc)) - timedelta(days=90)) < timedelta(minutes=5)
    assert sync.PROTOCOL == 2


def test_peer_routes_need_valid_key(client, giver):
    key = giver["key"]["key"]
    assert client.get("/sync/peer/hello").status_code == 401
    assert client.get("/sync/peer/hello", headers={"X-KH-Sync-Key": key + "x"}).status_code == 401
    h = client.get("/sync/peer/hello", headers={"X-KH-Sync-Key": key}).json()
    assert h["protocol"] == 2 and h["dim"] == 384 and h["model"] == "kh-hash-384" and len(h["probe"]) == 384
    man = client.get("/sync/peer/manifest", headers={"X-KH-Sync-Key": key}).json()
    assert [p["id"] for p in man["projects"]] == [giver["project"]]
    assert man["projects"][0]["parts"]["knowledge"] == 2
    assert set(man["projects"][0]["parts"]) == set(sync.DEFAULT_PARTS)
    ver = client.post("/sync/peer/verify", headers={"X-KH-Sync-Key": key}).json()
    assert ver == {"role": "reader", "projects": [giver["project"]]}
    # export: offered project only, window filter, paging
    hk = {"X-KH-Sync-Key": key}
    assert client.post("/sync/peer/export", json={"project": "orchestrator", "part": "knowledge"},
                       headers=hk).status_code == 403
    ex = client.post("/sync/peer/export", json={"project": giver["project"], "part": "knowledge"}, headers=hk).json()
    assert ex["total"] == 2 and {r["id"] for r in ex["rows"]} == set(giver["ids"])
    assert len(ex["rows"][0]["embedding"]) == 384 and ex["next_offset"] is None
    ex = client.post("/sync/peer/export", json={"project": giver["project"], "part": "knowledge",
                                                "since": "2026-10-10T00:00:00Z"}, headers=hk).json()
    assert [r["id"] for r in ex["rows"]] == [giver["ids"][1]]
    ex = client.post("/sync/peer/export", json={"project": giver["project"], "part": "knowledge", "limit": 1},
                     headers=hk).json()
    assert len(ex["rows"]) == 1 and ex["next_offset"] == 1
    lg = client.post("/sync/peer/export", json={"project": giver["project"], "part": "log"}, headers=hk).json()
    assert giver["log"] in [r["id"] for r in lg["rows"]]                 # hot window included
    # revoke
    kid = giver["key"]["kid"]
    assert client.delete(f"/sync/keys/{kid}").json() == {"revoked": kid}
    assert client.delete(f"/sync/keys/{kid}").status_code == 404
    r = client.get("/sync/peer/hello", headers=hk)
    assert r.status_code == 401 and r.json()["detail"] == "sync key revoked"


def test_merge_rules(client, uid):
    p = f"mg-{uid}"
    rows = [{"id": "r1", "text": "one", "metadata": {"ts": "2026-10-01T00:00:00Z", "a": 1}, "embedding": None}]
    assert sync.merge_rows(p, "knowledge", rows) == {"created": 1, "unchanged": 0, "kept": 0, "updated": 0}
    assert sync.merge_rows(p, "knowledge", rows) == {"created": 0, "unchanged": 1, "kept": 0, "updated": 0}
    older = [{"id": "r1", "text": "one old", "metadata": {"ts": "2026-09-01T00:00:00Z"}, "embedding": None}]
    assert sync.merge_rows(p, "knowledge", older)["kept"] == 1
    newer = [{"id": "r1", "text": "one new", "metadata": {"ts": "2026-10-05T00:00:00Z"}, "embedding": None}]
    assert sync.merge_rows(p, "knowledge", newer, dry=True)["updated"] == 1
    h = vs.get_existing(vs.part_collection_name("knowledge", p))
    assert vs.get_ids_vt(h, ["r1"])[0]["source_text"] == "one"            # dry run wrote nothing
    assert sync.merge_rows(p, "knowledge", newer)["updated"] == 1
    assert vs.get_ids_vt(h, ["r1"])[0]["source_text"] == "one new"


def test_connect_and_pull_from_self(client, giver, monkeypatch):
    monkeypatch.setattr(sync, "PEER_SESSION", client)
    key = giver["key"]["key"]
    assert client.post("/sync/connect", json={"key": "nonsense"}).status_code == 422
    c = client.post("/sync/connect", json={"key": key}).json()
    assert c["protocol"] == 2 and c["probe_cosine"] >= 0.999 and c["giver"] == "http://testserver"
    assert [p["id"] for p in c["manifest"]["projects"]] == [giver["project"]]
    assert client.post("/sync/pull", json={"key": key, "selection": []}).status_code == 422
    # giver and receiver are the same KH here: every row merges as unchanged (merge rules: test_merge_rules)
    out = sync.start_pull(key, [{"project": giver["project"], "parts": ["knowledge"]},
                                {"project": "orchestrator"}], wait=True)
    assert re.fullmatch(r"sj_[0-9a-f]{12}", out["job_id"]) and out["state"] == "done"
    job = client.get(f"/sync/jobs/{out['job_id']}").json()
    k = job["doc"]["projects"][giver["project"]]["parts"]["knowledge"]
    assert k["created"] == 0 and k["unchanged"] == 2 and k["pages"] == 1
    assert job["doc"]["projects"]["orchestrator"] == {"error": "not offered by the giver"}
    assert job["doc"]["projects"][giver["project"]]["registry"] == "merged"
    assert out["job_id"] in [j["id"] for j in client.get("/sync/jobs").json()["jobs"]]
    given = client.get("/sync/given").json()["given"]
    assert any(g["job_id"] == out["job_id"] and g["state"] == "done" for g in given)
    assert client.post(f"/sync/jobs/{out['job_id']}/cancel").json() == {"id": out["job_id"], "cancel": False}
    assert client.get("/sync/jobs/sj_nope").status_code == 404
    # the route answers 202 with a queued job (the background run itself is stubbed here)
    started = []
    monkeypatch.setattr(sync, "run_pull", lambda *a, **kw: started.append(a))
    r = client.post("/sync/pull", json={"key": key, "selection": [{"project": giver["project"],
                                                                    "parts": ["knowledge"]}], "dry_run": True})
    assert r.status_code == 202 and r.json()["state"] == "queued"
    assert sync.get_job(r.json()["job_id"])["state"] == "queued"


def test_grants(client, uid):
    from app import sqldb

    with sync._conn() as c:
        sqldb.upsert(c, "sync_grants", ["email", "project"],
                     {"email": f"r{uid}@x.com", "project": f"g-{uid}", "source": "u", "kid": "k", "job_id": "j",
                      "granted_at": iso()})
    assert sync.key_reads(f"r{uid}@x.com") == [f"g-{uid}"]
    assert any(g["email"] == f"r{uid}@x.com" for g in client.get("/sync/grants").json()["grants"])
    assert client.post("/sync/grants/remove", json={"email": f"r{uid}@x.com"}).json() == {"removed": 1}
    assert sync.key_reads(f"r{uid}@x.com") == []


def test_sync_me_and_auth(client, monkeypatch):
    assert client.get("/sync/me").json()["maintainer"] is True
    enable_signin(monkeypatch)
    m = bearer(make_token("mem@x.com"))
    j = client.get("/sync/me", headers=m).json()
    assert j["maintainer"] is False and j["email"] == "mem@x.com"
    assert client.get("/sync/me").status_code == 401
    assert client.get("/sync/keys", headers=m).status_code == 403
    assert client.post("/sync/keys", json={"label": "x"}, headers=m).status_code == 403
    assert client.get("/sync/grants", headers=m).status_code == 403
    assert client.get("/sync/jobs", headers=m).status_code == 200
    assert client.get("/sync/peer/hello").status_code == 401          # public route, the key is the credential
    mt = bearer(make_token("boss@x.com", role="maintainer"))
    assert client.get("/sync/keys", headers=mt).status_code == 200
