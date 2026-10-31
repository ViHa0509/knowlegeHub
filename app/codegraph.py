"""Codegraph (spec §4.12): tool-agnostic import of a code graph already converted to the KH contract.

Contract: node `{id, label, code_type, attr_type, source_file, source_location}`, edge
`{source, target, relation, confidence, source_file?, source_location?}`.

`POST /codegraph/import {source_id, tool, nodes, edges, topic="codegraph", conflicts_with[], register_source=true,
dry_run, vectorize_detail=false, exclude_code_types}` (project.write on source_id): ids are namespaced
`<source_id>__<id>` in vectors (the project's knowledge collection) and in the per-project graph; only summary
node types are embedded unless `vectorize_detail`; basic blocks are dropped by default.
`POST /codegraph/federated {term, limit, contract_path | nodes, edges}` (maintainer) searches a contract by label
without importing.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app import auth, projects
from app import vector_store as vs
from app.embeddings import EmbedderUnavailable, get_embedder
from app.graph_db import get_project_graph

log = logging.getLogger(__name__)

SUMMARY_TYPES = frozenset({"module", "file", "class", "interface", "service", "community", "package", "namespace",
                           "component", "struct", "enum", "trait"})
DEFAULT_EXCLUDE = ("basic_block",)
_CONF_WORDS = {"extracted": 1.0, "inferred": 0.7, "ambiguous": 0.3}
_ENCODE_BATCH = 64


class ImportRequest(BaseModel):
    source_id: str = Field(..., min_length=1)
    tool: Optional[str] = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    topic: str = "codegraph"
    conflicts_with: list[str] = Field(default_factory=list)
    register_source: bool = True
    dry_run: bool = False
    vectorize_detail: bool = False
    exclude_code_types: Optional[list[str]] = None


class FederatedRequest(BaseModel):
    term: str = Field(..., min_length=1)
    limit: int = Field(20, ge=1, le=500)
    contract_path: Optional[str] = None
    nodes: Optional[list[dict[str, Any]]] = None
    edges: Optional[list[dict[str, Any]]] = None


def _code_type(n: dict) -> str:
    return str(n.get("code_type") or "").strip().lower()


def _relation(r) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", str(r or "RELATED_TO")).strip("_").upper()
    return s or "RELATED_TO"


def _confidence(v) -> float:
    if v is None or v == "":
        return 1.0
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return _CONF_WORDS.get(str(v).strip().lower(), 1.0)


def _node_text(n: dict) -> str:
    where = n.get("source_file") or ""
    if where and n.get("source_location"):
        where = f"{where}:{n.get('source_location')}"
    parts = [_code_type(n) or "node", str(n.get("label") or n.get("id"))]
    if n.get("attr_type"):
        parts.append(f"[{n.get('attr_type')}]")
    if where:
        parts.append(f"in {where}")
    return " ".join(parts)


def _filter(body: ImportRequest) -> tuple[list[dict], list[dict], dict]:
    exclude = {str(t).strip().lower() for t in (body.exclude_code_types if body.exclude_code_types is not None
                                                 else DEFAULT_EXCLUDE)}
    nodes: dict[str, dict] = {}
    dropped = {"excluded": 0, "invalid": 0, "duplicate": 0}
    for n in body.nodes:
        nid = str(n.get("id") or "").strip()
        if not nid:
            dropped["invalid"] += 1
            continue
        if _code_type(n) in exclude:
            dropped["excluded"] += 1
            continue
        if nid in nodes:
            dropped["duplicate"] += 1
        nodes[nid] = {**n, "id": nid}
    edges = []
    e_dropped = 0
    for e in body.edges:
        s, t = str(e.get("source") or "").strip(), str(e.get("target") or "").strip()
        if not s or not t or s not in nodes or t not in nodes:
            e_dropped += 1
            continue
        edges.append(e)
    dropped["edges"] = e_dropped
    return list(nodes.values()), edges, dropped


def import_graph(body: ImportRequest, model=None) -> dict:
    try:
        sid = projects.validate_id(body.source_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if sid != body.source_id.strip():
        raise HTTPException(status_code=400, detail=f"source_id must be a canonical project id (use '{sid}')")
    tool = (body.tool or "").strip() or None
    topic = (body.topic or "codegraph").strip() or "codegraph"
    nodes, edges, dropped = _filter(body)
    to_embed = [n for n in nodes if body.vectorize_detail or _code_type(n) in SUMMARY_TYPES]

    def ns(i) -> str:
        return f"{sid}__{i}"

    out = {"status": "ok", "source_id": sid, "tool": tool, "topic": topic, "dry_run": body.dry_run,
           "nodes_received": len(body.nodes), "edges_received": len(body.edges), "nodes_kept": len(nodes),
           "edges_kept": len(edges), "dropped": dropped, "to_vectorize": len(to_embed),
           "vectors_written": 0, "graph_nodes": 0, "graph_edges": 0, "registered": False}
    if body.dry_run:
        return out
    if body.register_source:
        try:
            projects.get_or_create_project(sid, tools=[tool] if tool else (), conflicts_with=body.conflicts_with)
            out["registered"] = True
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    p = auth.current()
    base_attrs = {"source_id": sid, "topic": topic}
    if tool:
        base_attrs["tool"] = tool
    g_nodes = []
    for n in nodes:
        attrs = dict(base_attrs)
        for k in ("attr_type", "source_file", "source_location"):
            if n.get(k) not in (None, ""):
                attrs[k] = n.get(k)
        attrs["code_type"] = _code_type(n)
        attrs["id"] = str(n.get("id"))
        g_nodes.append({"core_id": ns(n["id"]), "type": _code_type(n) or "node",
                        "label": str(n.get("label") or n.get("id")), "attributes": attrs,
                        "source_text": _node_text(n)})
    g_edges = []
    for e in edges:
        attrs = dict(base_attrs)
        for k in ("source_file", "source_location"):
            if e.get(k) not in (None, ""):
                attrs[k] = e.get(k)
        if isinstance(e.get("confidence"), str):
            attrs["confidence_label"] = e.get("confidence")
        g_edges.append({"source_core_id": ns(e["source"]), "relation": _relation(e.get("relation")),
                        "target_core_id": ns(e["target"]), "attributes": attrs,
                        "confidence": _confidence(e.get("confidence"))})
    try:
        g = get_project_graph(sid)
        out["graph_nodes"] = g.upsert_nodes(g_nodes)
        out["graph_edges"] = g.upsert_edges(g_edges)
    except Exception as e:  # noqa: BLE001
        log.exception("codegraph graph write failed")
        raise HTTPException(status_code=500, detail=f"Graph store error: {e}")
    if to_embed:
        enc = model if model is not None and hasattr(model, "encode") else get_embedder()
        coll = vs.scope_collection("knowledge", sid)
        for i in range(0, len(to_embed), _ENCODE_BATCH):
            part = to_embed[i:i + _ENCODE_BATCH]
            texts = [_node_text(n) for n in part]
            try:
                vecs = enc.encode(texts, normalize_embeddings=True)
            except EmbedderUnavailable:
                raise
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=500, detail=f"Embedding failed: {e}")
            metas = []
            for n in part:
                m = {"knowledge_type": "quick", "scope": "knowledge", "project": sid, "source_id": sid,
                     "topic": topic, "code_type": _code_type(n), "label": str(n.get("label") or n.get("id")),
                     "source_file": n.get("source_file") or "", "source_location": str(n.get("source_location") or ""),
                     "core_id": ns(n["id"])}
                if tool:
                    m["tool"] = tool
                if p is not None:
                    m["author"] = p.email
                metas.append(m)
            try:
                vs.upsert_rows(coll, [ns(n["id"]) for n in part], list(vecs), texts, metas)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=500, detail=f"Vector store error: {e}")
            out["vectors_written"] += len(part)
    return out


def _load_contract(body: FederatedRequest) -> tuple[str, list[dict], list[dict]]:
    if body.contract_path:
        path = Path(body.contract_path)
        if not path.is_file():
            raise HTTPException(status_code=404, detail=f"no contract file at {body.contract_path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"contract file is not JSON: {e}")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="contract file must hold {nodes, edges}")
        return body.contract_path, list(data.get("nodes") or []), list(data.get("edges") or [])
    if body.nodes is None:
        raise HTTPException(status_code=400, detail="give contract_path or nodes")
    return "inline", list(body.nodes), list(body.edges or [])


def federated(body: FederatedRequest) -> dict:
    source, nodes, edges = _load_contract(body)
    term = body.term.strip().lower()
    hits = [n for n in nodes if isinstance(n, dict)
            and (term in str(n.get("label") or "").lower() or term in str(n.get("id") or "").lower())]
    hits.sort(key=lambda n: (str(n.get("label") or "").lower() != term, len(str(n.get("label") or ""))))
    hits = hits[:body.limit]
    ids = {str(n.get("id")) for n in hits}
    near = [e for e in edges if isinstance(e, dict) and (str(e.get("source")) in ids or str(e.get("target")) in ids)]
    return {"term": body.term, "source": source, "count": len(hits), "nodes": hits, "edges": near}


def attach(app: FastAPI) -> None:
    @app.post("/codegraph/import", tags=["codegraph"])
    def codegraph_import(body: ImportRequest, request: Request):
        return import_graph(body, getattr(request.app.state, "model", None))

    @app.post("/codegraph/federated", tags=["codegraph"])
    def codegraph_federated(body: FederatedRequest):
        return federated(body)
