"""§4.12 smaller admin surfaces: revisions, download sources, gallery, codegraph."""
from __future__ import annotations

import base64
import json
import uuid

import pytest

from app import auth
from app import vector_store as vs
from tests.auth_helpers import bearer, make_token


def _uid() -> str:
    return uuid.uuid4().hex[:8]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# ---------------------------------------------------------------------------
# Rule table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,kind", [
    ("POST", "/knowledge/revise", "project"), ("POST", "/knowledge/history", "project"),
    ("GET", "/downloads", "maintainer"), ("PUT", "/downloads/kh-helper", "maintainer"),
    ("DELETE", "/downloads/kh-helper", "maintainer"),
    ("GET", "/gallery/designs", "public"), ("GET", "/gallery/designs/12/thumb", "public"),
    ("GET", "/gallery/designs/12/demo", "public"), ("PUT", "/gallery/designs/12", "project"),
    ("DELETE", "/gallery/designs/12", "maintainer"),
    ("POST", "/codegraph/import", "project"), ("POST", "/codegraph/federated", "maintainer"),
])
def test_rules_exist(method, path, kind):
    rule, _ = auth.match_rule(method, path)
    assert rule is not None and rule["kind"] == kind


# ---------------------------------------------------------------------------
# Revisions
# ---------------------------------------------------------------------------


def test_revise_supersedes_and_history(client):
    pid, subj = f"rev-{_uid()}", f"db-host-{_uid()}"
    r1 = client.post("/knowledge/revise", json={"subject": subj, "text": "the db host is alpha", "project": pid,
                                                "ts": "2026-10-01T00:00:00Z", "author": "a@x.com"})
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["superseded"] == [] and b1["chain_len"] == 1 and b1["subject"] == subj
    assert b1["revision_id"].startswith(f"rev_{subj}_") and len(b1["revision_id"]) == len(f"rev_{subj}_") + 12
    r2 = client.post("/knowledge/revise", json={"subject": subj, "text": "the db host is beta", "project": pid,
                                                "ts": "2026-10-02T00:00:00Z"}).json()
    assert r2["superseded"] == [b1["revision_id"]] and r2["chain_len"] == 2
    h = client.post("/knowledge/history", json={"subject": subj, "project": pid}).json()
    assert h["count"] == 2 and h["current"] == r2["revision_id"]
    cur, old = h["revisions"]
    assert cur["current"] is True and cur["text"] == "the db host is beta" and cur["superseded_by"] == ""
    assert old["current"] is False and old["superseded_by"] == r2["revision_id"] and old["author"] == "a@x.com"
    # rows live in the project's knowledge collection, old one still quick
    rows = dict(vs.meta_rows_vt(vs.get_existing(f"kh_know_{pid}")))
    assert rows[b1["revision_id"]]["rev_current"] == "0" and rows[b1["revision_id"]]["knowledge_type"] == "quick"
    assert rows[r2["revision_id"]]["revsubject"] == subj


def test_history_of_unknown_subject_is_empty(client):
    h = client.post("/knowledge/history", json={"subject": "nothing-" + _uid(), "project": "nowhere-" + _uid()})
    assert h.status_code == 200 and h.json()["count"] == 0 and h.json()["current"] is None


def test_revise_needs_write(signed_client):
    c = signed_client
    pid = f"rev-{_uid()}"
    reader = bearer(make_token("r@x.com", projects={pid: "readonly"}))
    r = c.post("/knowledge/revise", json={"subject": "s", "text": "t", "project": pid}, headers=reader)
    assert r.status_code == 403
    assert c.post("/knowledge/history", json={"subject": "s", "project": pid}, headers=reader).status_code == 200
    writer = bearer(make_token("w@x.com", projects={pid: "editable"}))
    r = c.post("/knowledge/revise", json={"subject": "s", "text": "t", "project": pid, "author": "fake@x.com"},
               headers=writer)
    assert r.status_code == 200
    h = c.post("/knowledge/history", json={"subject": "s", "project": pid}, headers=writer).json()
    assert h["revisions"][0]["author"] == "w@x.com"


# ---------------------------------------------------------------------------
# Download sources
# ---------------------------------------------------------------------------


def test_downloads_crud_and_auth_download(client):
    mod = f"mod-{_uid()}"
    r = client.put(f"/downloads/{mod}", json={"type": "https", "path": "https://example.com/x.zip",
                                              "version": "1.2.3", "file_name": "x.zip"})
    assert r.status_code == 200 and r.json()["source"]["type"] == "https"
    items = client.get("/downloads").json()["sources"]
    assert any(s["module"] == mod and s["version"] == "1.2.3" for s in items)
    d = client.get(f"/auth/download/{mod}")
    assert d.status_code == 200 and d.json()["url"] == "https://example.com/x.zip" and d.json()["version"] == "1.2.3"
    assert client.delete(f"/downloads/{mod}").json() == {"deleted": mod}
    assert client.delete(f"/downloads/{mod}").status_code == 404


@pytest.mark.parametrize("body", [
    {"type": "ftp", "path": "x"},
    {"type": "https", "path": "http://insecure"},
    {"type": "local", "path": "../../etc/passwd"},
    {"type": "local", "path": ""},
    {"type": "https", "path": "https://a/b", "sha256": "nothex"},
])
def test_downloads_validation(client, body):
    assert client.put("/downloads/bad", json=body).status_code == 400


def test_downloads_maintainer_only(signed_client):
    member = bearer(make_token("m@x.com"))
    assert signed_client.get("/downloads", headers=member).status_code == 403
    boss = bearer(make_token("boss@x.com", role="maintainer"))
    assert signed_client.get("/downloads", headers=boss).status_code == 200


# ---------------------------------------------------------------------------
# Gallery
# ---------------------------------------------------------------------------

DEMO = b"<html><body>demo</body></html>"
THUMB = b"RIFF\x00\x00\x00\x00WEBPVP8 "


def _design(**kw):
    body = {"name": "Glass", "kind": "style", "desc": "frosted", "tags": ["glass", "blur"],
            "demo_b64": _b64(DEMO), "thumb_b64": _b64(THUMB),
            "variants": [{"label": "dark", "demo_b64": _b64(b"<html>dark</html>")}]}
    body.update(kw)
    return body


def test_gallery_put_list_thumb_demo_delete(client):
    r = client.put("/gallery/designs/42", json=_design())
    assert r.status_code == 200 and r.json()["variants"] == 1
    lst = client.get("/gallery/designs")
    assert lst.headers["access-control-allow-origin"] == "*"
    d = next(x for x in lst.json()["designs"] if x["no"] == "42")
    assert d["name"] == "Glass" and d["tags"] == ["glass", "blur"] and "thumb" not in d
    assert d["variants"] == [{"idx": 0, "label": "dark"}]
    d = next(x for x in client.get("/gallery/designs?thumbs=1").json()["designs"] if x["no"] == "42")
    assert d["thumb"] == "data:image/webp;base64," + _b64(THUMB)
    t = client.get("/gallery/designs/42/thumb")
    assert t.status_code == 200 and t.headers["content-type"] == "image/webp" and t.content == THUMB
    h = client.get("/gallery/designs/42/demo")
    assert h.status_code == 200 and h.content == DEMO
    assert h.headers["content-security-policy"] == "sandbox allow-scripts allow-popups allow-forms allow-modals"
    assert client.get("/gallery/designs/42/demo?v=0").content == b"<html>dark</html>"
    assert client.get("/gallery/designs/42/demo?v=5").status_code == 404
    # write drops the cache
    client.put("/gallery/designs/42", json=_design(name="Glass 2", variants=[]))
    d = next(x for x in client.get("/gallery/designs").json()["designs"] if x["no"] == "42")
    assert d["name"] == "Glass 2" and d["variants"] == []
    assert client.delete("/gallery/designs/42").json() == {"deleted": "42"}
    assert client.get("/gallery/designs/42/thumb").status_code == 404
    assert client.delete("/gallery/designs/42").status_code == 404


@pytest.mark.parametrize("no,kw,status", [
    ("7", {}, 400), ("1234", {}, 400), ("55", {"kind": "color"}, 400), ("55", {"demo_b64": "!!!"}, 400),
])
def test_gallery_validation(client, no, kw, status):
    assert client.put(f"/gallery/designs/{no}", json=_design(**kw)).status_code == status


def test_gallery_size_cap(client, monkeypatch):
    monkeypatch.setenv("KH_GALLERY_MAX_MB", "0.00001")
    assert client.put("/gallery/designs/56", json=_design()).status_code == 413


def test_gallery_permissions(signed_client):
    c = signed_client
    assert c.get("/gallery/designs").status_code == 200  # public
    member = bearer(make_token("m@x.com"))
    assert c.put("/gallery/designs/57", json=_design(), headers=member).status_code == 403
    designer = bearer(make_token("d@x.com", projects={"designer": "editable"}))
    assert c.put("/gallery/designs/57", json=_design(), headers=designer).status_code == 200
    assert c.delete("/gallery/designs/57", headers=designer).status_code == 403
    boss = bearer(make_token("boss@x.com", role="maintainer"))
    assert c.delete("/gallery/designs/57", headers=boss).status_code == 200


# ---------------------------------------------------------------------------
# Codegraph
# ---------------------------------------------------------------------------

NODES = [
    {"id": "m1", "label": "billing", "code_type": "module", "source_file": "billing/__init__.py"},
    {"id": "c1", "label": "Invoice", "code_type": "class", "source_file": "billing/invoice.py",
     "source_location": "L10"},
    {"id": "f1", "label": "Invoice.total", "code_type": "function", "source_file": "billing/invoice.py"},
    {"id": "b1", "label": "block 3", "code_type": "basic_block"},
]
EDGES = [
    {"source": "c1", "target": "m1", "relation": "belongs to", "confidence": "EXTRACTED"},
    {"source": "f1", "target": "c1", "relation": "MEMBER_OF", "confidence": 0.9},
    {"source": "b1", "target": "f1", "relation": "PART_OF"},
]


def test_codegraph_dry_run(client):
    pid = f"cg-{_uid()}"
    r = client.post("/codegraph/import", json={"source_id": pid, "tool": "graphify", "nodes": NODES, "edges": EDGES,
                                               "dry_run": True})
    b = r.json()
    assert r.status_code == 200 and b["dry_run"] is True
    assert b["nodes_kept"] == 3 and b["edges_kept"] == 2 and b["to_vectorize"] == 2 and b["vectors_written"] == 0
    assert vs.get_existing(f"kh_know_{pid}") is None


def test_codegraph_import_writes_graph_and_vectors(client):
    from app.graph_db import get_project_graph

    pid = f"cg-{_uid()}"
    b = client.post("/codegraph/import", json={"source_id": pid, "tool": "graphify", "nodes": NODES,
                                               "edges": EDGES}).json()
    assert b["graph_nodes"] == 3 and b["graph_edges"] == 2 and b["vectors_written"] == 2 and b["registered"]
    ids = {rid for rid, _m in vs.meta_rows_vt(vs.get_existing(f"kh_know_{pid}"))}
    assert ids == {f"{pid}__m1", f"{pid}__c1"}
    nodes, edges = get_project_graph(pid).fetch_graph([f"{pid}__c1"], [], 1)
    assert any(e["relation"] == "BELONGS_TO" for e in edges)
    assert client.get(f"/projects/{pid}").status_code == 200
    # detail nodes too when asked; nothing excluded
    b = client.post("/codegraph/import", json={"source_id": pid, "nodes": NODES, "edges": EDGES,
                                               "vectorize_detail": True, "exclude_code_types": []}).json()
    assert b["vectors_written"] == 4 and b["edges_kept"] == 3


@pytest.mark.parametrize("sid", ["graphify__x", "gitnexus__y", "  "])
def test_codegraph_rejects_tool_prefixed_ids(client, sid):
    r = client.post("/codegraph/import", json={"source_id": sid, "nodes": NODES, "edges": []})
    assert r.status_code in (400, 422)


def test_codegraph_federated_inline_and_file(client, tmp_path):
    r = client.post("/codegraph/federated", json={"term": "invoice", "nodes": NODES, "edges": EDGES})
    b = r.json()
    assert r.status_code == 200 and b["count"] == 2 and b["nodes"][0]["label"] == "Invoice"
    assert len(b["edges"]) == 3
    f = tmp_path / "contract.json"
    f.write_text(json.dumps({"nodes": NODES, "edges": EDGES}), encoding="utf-8")
    b = client.post("/codegraph/federated", json={"term": "billing", "limit": 1, "contract_path": str(f)}).json()
    assert b["count"] == 1 and b["source"] == str(f)
    assert client.post("/codegraph/federated", json={"term": "x"}).status_code == 400
    assert client.post("/codegraph/federated", json={"term": "x", "contract_path": str(tmp_path / "no.json")}
                       ).status_code == 404


def test_codegraph_permissions(signed_client):
    c = signed_client
    pid = f"cg-{_uid()}"
    reader = bearer(make_token("r@x.com", projects={pid: "readonly"}))
    body = {"source_id": pid, "nodes": NODES, "edges": EDGES, "dry_run": True}
    assert c.post("/codegraph/import", json=body, headers=reader).status_code == 403
    writer = bearer(make_token("w@x.com", projects={pid: "editable"}))
    assert c.post("/codegraph/import", json=body, headers=writer).status_code == 200
    assert c.post("/codegraph/federated", json={"term": "x", "nodes": []}, headers=writer).status_code == 403
