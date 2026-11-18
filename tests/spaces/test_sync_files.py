"""POST /sync/dump and POST /sync/import (KH 7.1 local sync pages)."""
from __future__ import annotations

from app import projects


def test_dump_import_round_trip(client, fresh_store):
    projects.get_or_create_project("gamma")
    for t in ("Gamma fact one", "Gamma fact two"):
        assert client.post("/vector/add", json={"text": t, "project": "gamma"}).status_code == 200
    bad = client.post("/sync/dump", json={"part": "nope", "project": "gamma"})
    assert bad.status_code == 422 and bad.json()["detail"].startswith("part must be one of ['knowledge', 'log'")
    page = client.post("/sync/dump", json={"part": "knowledge", "project": "gamma", "limit": 1}).json()
    assert page["total"] == 2 and page["scanned"] == 1 and page["next_offset"] == 1
    assert set(page["rows"][0]) == {"id", "document", "metadata", "embedding"} and page["emb_dim"] > 0
    full = client.post("/sync/dump", json={"part": "knowledge", "project": "gamma"}).json()
    mism = dict(full, project="delta", emb_model="other-model")
    r = client.post("/sync/import", json=mism)
    assert r.status_code == 409 and "this KH runs" in r.json()["detail"]
    dry = client.post("/sync/import", json=dict(full, project="delta", dry_run=True)).json()
    assert dry["project_result"] == "would create" and not projects.exists("delta")
    imp = client.post("/sync/import", json=dict(full, project="delta")).json()
    assert imp["project_result"] == "created" and imp["counts"]["created"] == 2
    again = client.post("/sync/import", json=dict(full, project="delta")).json()
    assert again["project_result"] == "exists" and again["counts"]["unchanged"] == 2
    sc = {s["scope"]: s["rows"] for s in client.get("/spaces/delta/scopes").json()["scopes"]}
    assert sc["knowledges"] == 2
