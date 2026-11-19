"""KH 7.1 home admin routes: config base, embedding, review, retention, space gaps and learned links."""
from __future__ import annotations

import time

import pytest

from app import distill, projects
from app import vector_store as vs


def test_home_config_base_open_mode(client):
    j = client.get("/home/config").json()
    assert j["auth"] is False and j["base"] == "http://testserver"


def test_embedding_shape_and_writes_refused(client):
    j = client.get("/home/embedding").json()
    assert set(j) == {"active", "embedder", "backend", "maintenance", "notice", "models", "jobs", "backup_path",
                      "can_switch"}
    assert j["can_switch"] is False and j["models"] == [] and j["jobs"] == []
    for path, body in (("/home/embedding/convert", {"model_id": "x"}), ("/home/embedding/models/m1/check", None)):
        r = client.post(path, json=body) if body else client.post(path)
        assert r.status_code == 501 and "KH_EMBEDDING_MODEL" in r.json()["detail"]


def _proposal(project, text="Run the unit tests before pushing a release branch"):
    projects.get_or_create_project(project)
    distill._write(project, "pend1", {"kind": "lesson", "action": "release", "context": "-", "entities_json": "[]",
                                      "entity_key": "-", "dkey": "k1", "status": "proposed",
                                      "sources_json": "[\"l1\"]", "evidence_count": 1,
                                      "distilled_by": "tester", "distilled_at": "2026-01-01T00:00:00Z"},
                   "Test first", text)


def test_review_empty_and_apply_empty(client, fresh_store):
    assert client.get("/home/review").json() == {"shared_project": "lesson-learned", "projects": [], "count": 0}
    assert client.post("/home/review/apply", json={}).json() == {"results": []}


def test_review_apply_approve_and_reject(client, fresh_store):
    _proposal("alpha")
    j = client.get("/home/review").json()
    assert j["count"] == 1 and j["projects"][0]["project"] == "alpha"
    it = j["projects"][0]["items"][0]
    assert it["id"] == "pend1" and it["title"] == "Test first" and it["can_share"] is True and it["sources"] == 1
    r = client.post("/home/review/apply", json={"decisions": [
        {"project": "alpha", "id": "pend1", "decision": "approve", "share": False, "title": "Test before push"},
        {"project": "alpha", "id": "nope", "decision": "reject"}]}).json()
    assert r["results"][0]["result"] == "active" and r["results"][1]["result"] == "unknown"
    assert distill.get_item("alpha", "pend1")["title"] == "Test before push"
    assert client.get("/home/review").json()["count"] == 0


def test_review_reject(client, fresh_store):
    _proposal("alpha")
    r = client.post("/home/review/apply", json={"decisions": [
        {"project": "alpha", "id": "pend1", "decision": "reject"}]}).json()
    assert r["results"][0]["result"] == "withdrawn"
    assert client.get("/home/review").json()["count"] == 0


def test_retention_off_by_default(client, fresh_store, monkeypatch):
    monkeypatch.delenv("KH_LOG_RETENTION_DAYS", raising=False)
    j = client.post("/home/retention/logs", json={"days": 0, "dry_run": True}).json()
    assert j["days"] == 0 and j["total"] == 0 and j["projects"] == [] and "off" in j["note"]


def test_retention_dry_run_and_delete(client, fresh_store):
    projects.get_or_create_project("alpha")
    old = client.post("/vector/add", json={"text": "old work log", "project": "alpha", "scope": "log",
                                           "metadata": {"ts": "2020-01-01T00:00:00Z"}}).json()["vector_id"]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    client.post("/vector/add", json={"text": "fresh work log", "project": "alpha", "scope": "log",
                                     "metadata": {"ts": now}})
    j = client.post("/home/retention/logs", json={"days": 1, "dry_run": True}).json()
    assert j["total"] == 1 and j["projects"] == [{"project": "alpha", "rows": 1}] and j["cut"]
    h = vs.get_existing(vs.part_collection_name("log", "alpha"))
    assert len(vs.get_ids_vt(h, [old])) == 1
    j = client.post("/home/retention/logs", json={"days": 1, "dry_run": False}).json()
    assert j["total"] == 1 and j["dry_run"] is False
    assert vs.get_ids_vt(h, [old]) == []
    assert client.post("/home/retention/logs", json={"days": 1, "dry_run": True}).json()["total"] == 0


def test_space_gaps_and_learned_links(client, fresh_store):
    projects.get_or_create_project("alpha")
    assert client.get("/spaces/alpha/gaps", params={"open": "true", "limit": 5}).json() == {
        "space": "alpha", "open": True, "total": 0, "items": []}
    assert client.get("/spaces/alpha/links/learned", params={"limit": 5}).json() == {
        "space": "alpha", "total": 0, "offset": 0, "limit": 5, "items": []}
    for p in ("/spaces/nosuch/gaps", "/spaces/nosuch/links/learned"):
        r = client.get(p)
        assert r.status_code == 404 and "no such space" in r.json()["detail"]
