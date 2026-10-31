"""§1.7 legacy orchestrator source lanes + Appendix B LEGACY routes /orchestrator/hybrid|graph_read|graph_traverse."""
from __future__ import annotations

import uuid

import pytest

from app import auth, projects
from app import vector_store as vs
from app.embeddings import get_embedder
from app.graph_db import get_project_graph


def _pid(prefix: str = "legacy") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _enc(text: str):
    return get_embedder().encode(text, normalize_embeddings=True)


ROWS = [
    ("r1", "def parse_invoice(doc): reads the invoice header", {"core_id": "inv"}),
    ("r2", "class PaymentGateway handles card payments", {"core_id": "pay"}),
    ("r3", "parse_invoice helper for totals", {"core_id": "inv"}),
    ("r4", "unrelated weather notes about rain", {}),
]


def _seed(pid: str) -> None:
    projects.get_or_create_project(pid)
    vs.upsert_src_batch(pid, [r[0] for r in ROWS], [_enc(r[1]) for r in ROWS], [r[1] for r in ROWS],
                        [r[2] for r in ROWS])


# ---------------------------------------------------------------------------
# Rule table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/orchestrator/hybrid", "/orchestrator/graph_read", "/orchestrator/graph_traverse"])
def test_rules(path):
    rule, _ = auth.match_rule("POST", path)
    assert rule is not None and rule["kind"] == "project" and rule["permission"] == "project.read"
    assert rule["project_from"] == ["body.source_id"]


# ---------------------------------------------------------------------------
# Lane functions
# ---------------------------------------------------------------------------


def test_keyword_terms():
    t = vs.keyword_terms("where is the parse_invoice in PaymentGateway for a db x1 parse_invoice")
    assert t == ["parse_invoice", "PaymentGateway"]
    many = vs.keyword_terms(" ".join(f"term{i}" for i in range(20)))
    assert len(many) == vs.KEYWORD_MAX_TERMS == 8
    assert vs.keyword_where_document("only parse_invoice") == {"$contains": "parse_invoice"}
    assert vs.keyword_where_document("parse_invoice PaymentGateway") == {
        "$or": [{"$contains": "parse_invoice"}, {"$contains": "PaymentGateway"}]}
    assert vs.keyword_where_document("the and of") is None


def test_upsert_chunks_and_docs(monkeypatch):
    pid = _pid()
    monkeypatch.setattr(vs, "SRC_UPSERT_CHUNK", 2)
    calls = []
    coll = vs.src_collection(pid)
    real = coll.upsert

    def spy(**kw):
        calls.append(kw)
        return real(**kw)

    monkeypatch.setattr(coll, "upsert", spy)
    monkeypatch.setattr(vs, "src_collection", lambda s: coll)
    n = vs.upsert_src_batch(pid, ["a", "b", "c"], [_enc("x"), _enc("y"), _enc("z")], ["x", "", "z"],
                            [{}, {}, {}])
    assert n == 3 and [len(c["ids"]) for c in calls] == [2, 1]
    assert calls[0]["documents"] is None and calls[1]["documents"] == ["z"]
    assert calls[0]["metadatas"][0]["source_text"] == "x" and "source_text" not in calls[0]["metadatas"][1]


def test_query_src_retries_once_then_degrades(monkeypatch):
    pid = _pid()
    _seed(pid)
    attempts = []

    def boom(*a, **k):
        attempts.append(1)
        raise RuntimeError("transient")

    monkeypatch.setattr(vs, "query_vt", boom)
    assert vs.query_src(pid, _enc("invoice"), 3) == []
    assert len(attempts) == 2
    assert vs.query_src(_pid("nosuch"), _enc("x"), 3) == []


def test_hybrid_src_merges_by_core_id():
    pid = _pid()
    _seed(pid)
    cands = vs.hybrid_src(pid, "parse_invoice", _enc("parse_invoice"), k=10)
    by = {c["core_id"]: c for c in cands}
    assert len(cands) == len(by)  # merged by core_id
    assert by["inv"]["lanes"] == ["vector", "keyword"] and by["inv"]["matched_terms"] == ["parse_invoice"]
    assert by["inv"]["score"] is not None
    assert by["r4"]["lanes"] == ["vector"]
    kw_only = vs.hybrid_src(pid, "PaymentGateway", None, k=10)
    assert [c["core_id"] for c in kw_only] == ["pay"] and kw_only[0]["lanes"] == ["keyword"]
    assert kw_only[0]["score"] is None


def test_hub_descriptor_roundtrip():
    pid = _pid()
    vs.hub_upsert(pid, _enc("invoice service"), "invoice service source", {"tool": "graphify"})
    row = vs.hub_get(pid)
    assert row["source_text"] == "invoice service source"
    assert row["metadata"]["source_id"] == pid and row["metadata"]["kind"] == "source-descriptor"
    assert any(h["vector_id"] == pid for h in vs.hub_query(_enc("invoice service"), k=50))
    assert vs.hub_delete(pid) == [pid] and vs.hub_get(pid) is None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_hybrid_route(client):
    pid = _pid()
    _seed(pid)
    vs.hub_upsert(pid, _enc("billing code"), "billing code", {})
    r = client.post("/orchestrator/hybrid", json={"source_id": pid, "query": "where is parse_invoice", "k": 5})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["source_id"] == pid and b["collection"] == f"kh_src_{pid}" and b["terms"] == ["parse_invoice"]
    assert b["vector_lane"] == "ok" and b["count"] == len(b["candidates"])
    inv = next(c for c in b["candidates"] if c["core_id"] == "inv")
    assert "keyword" in inv["lanes"]
    assert b["hub"]["vector_id"] == pid


def test_hybrid_route_errors(client):
    assert client.post("/orchestrator/hybrid", json={"source_id": _pid("nope"), "query": "x"}).status_code == 404
    pid = _pid()
    projects.get_or_create_project(pid)
    assert client.post("/orchestrator/hybrid", json={"source_id": pid}).status_code == 400
    empty = client.post("/orchestrator/hybrid", json={"source_id": pid, "query": "anything"}).json()
    assert empty["candidates"] == [] and empty["hub"] is None


def _graph(pid: str) -> None:
    projects.get_or_create_project(pid)
    g = get_project_graph(pid)
    g.upsert_nodes([{"core_id": c, "label": c, "type": "Artifact", "attributes": {}, "source_text": c}
                    for c in ("app", "mod", "fn", "lib")])
    g.upsert_edges([
        {"source_core_id": "mod", "relation": "PART_OF", "target_core_id": "app"},
        {"source_core_id": "fn", "relation": "PART_OF", "target_core_id": "mod"},
        {"source_core_id": "fn", "relation": "CALLS", "target_core_id": "lib"},
    ])


def test_graph_read_route(client):
    pid = _pid()
    _graph(pid)
    r = client.post("/orchestrator/graph_read", json={"source_id": pid, "core_ids": ["app", "ghost"]})
    assert r.status_code == 200, r.text
    b = r.json()
    ids = {n["core_id"] for n in b["nodes"]}
    assert {"app", "mod", "fn", "lib"} <= ids and b["missing"] == ["ghost"]
    assert b["count"] == {"nodes": len(b["nodes"]), "edges": len(b["edges"])} and b["count"]["edges"] == 3
    r0 = client.post("/orchestrator/graph_read", json={"source_id": pid, "core_ids": ["app"], "hops": 0}).json()
    assert {n["core_id"] for n in r0["nodes"]} == {"app", "mod"}
    assert client.post("/orchestrator/graph_read", json={"source_id": pid, "core_ids": []}).status_code == 422
    assert client.post("/orchestrator/graph_read",
                       json={"source_id": _pid("nope"), "core_ids": ["a"]}).status_code == 404


def test_graph_traverse_route(client):
    pid = _pid()
    _graph(pid)
    b = client.post("/orchestrator/graph_traverse", json={"source_id": pid, "start_ids": ["fn"]}).json()
    assert b["count"]["edges"] == 3 and set(b["reached"]) == {"mod", "lib", "app"}
    b2 = client.post("/orchestrator/graph_traverse",
                     json={"source_id": pid, "start_ids": ["fn"], "relations": ["calls"], "max_depth": 2}).json()
    assert b2["relations"] == ["CALLS"] and b2["reached"] == ["lib"]
    assert client.post("/orchestrator/graph_traverse",
                       json={"source_id": "graphify__x", "start_ids": ["a"]}).status_code == 400
