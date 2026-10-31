"""Orchestrator routes (spec §2.10, §3.10): `GET /orchestrator/project_match` (literal, no embedder), plus the
LEGACY source-lane routes of Appendix B (spec §1.7 "Legacy orchestrator lanes", rule table §2.5: read on
body.source_id):

    POST /orchestrator/hybrid          {source_id, query|text, k=10, where?}
        vector + keyword candidates over `kh_src_<source_id>` merged by core_id (`vector_store.hybrid_src`),
        no re-ranking; plus the source's hub descriptor from `kh_orch_hub` when one exists.
    POST /orchestrator/graph_read      {source_id, core_ids[], hops=3}
        `fetch_graph` on the source's project graph (hierarchy expansion + 1-hop edges).
    POST /orchestrator/graph_traverse  {source_id, start_ids[], relations?, max_depth=3}
        BFS over the source's project graph (`traverse_logic`).

Request/response shapes of the legacy routes are the rebuild's own (UNKNOWN in the spec; docs/deviations.md).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app import config, projects
from app import vector_store as vs

log = logging.getLogger(__name__)

EVERY_PROJECT = "every project"
MAX_GRAPH_IDS = 500


def project_match(text: str) -> dict:
    hits = projects.match_projects(text or "")
    best = projects.resolve_from_hits(hits)
    weak_only = bool(hits) and all(h["weak"] for h in hits)
    return {"text": text, "count": len(hits), "best": best,
            "ambiguous": bool(hits) and best is None and not weak_only, "weak_only": weak_only,
            "policy_project": best or EVERY_PROJECT, "matches": hits}


# ---------------------------------------------------------------------------
# Legacy source lanes
# ---------------------------------------------------------------------------


class HybridRequest(BaseModel):
    source_id: str = Field(..., min_length=1)
    query: Optional[str] = None
    text: Optional[str] = None
    k: int = Field(10, ge=1, le=100)
    where: Optional[dict[str, Any]] = None


class GraphReadRequest(BaseModel):
    source_id: str = Field(..., min_length=1)
    core_ids: list[str] = Field(..., min_length=1, max_length=MAX_GRAPH_IDS)
    hops: int = Field(config.ONTOLOGY_HOPS, ge=0, le=6)


class GraphTraverseRequest(BaseModel):
    source_id: str = Field(..., min_length=1)
    start_ids: list[str] = Field(..., min_length=1, max_length=MAX_GRAPH_IDS)
    relations: Optional[list[str]] = None
    max_depth: int = Field(3, ge=1, le=6)


def _source(source_id: str) -> str:
    try:
        sid = projects.validate_id(source_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if sid != config.ORCHESTRATOR_ID and not projects.exists(sid):
        raise HTTPException(404, f"unknown project '{sid}'")
    return sid


def _encode(request: Request, text: str):
    model = getattr(request.app.state, "model", None)
    if model is None:
        from app.embeddings import get_embedder

        model = get_embedder()
    return model.encode(text, normalize_embeddings=True)


def _relation(r: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", str(r)).strip("_").upper()


def _ids(items: list[str]) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in items if isinstance(s, str) and s.strip()))


def hybrid(request: Request, body: HybridRequest) -> dict:
    sid = _source(body.source_id)
    q = (body.query or body.text or "").strip()
    if not q:
        raise HTTPException(400, "query is required")
    vector, vector_lane = None, "ok"
    try:
        vector = _encode(request, q)
    except Exception as e:  # noqa: BLE001 - embedder unavailable: keyword lane only
        log.warning("hybrid: embedding failed (%s); vector lane skipped", e)
        vector_lane = "unavailable"
    cands = vs.hybrid_src(sid, q, vector, body.k, body.where)
    return {"source_id": sid, "collection": vs.src_collection_name(sid), "query": q,
            "terms": vs.keyword_terms(q), "vector_lane": vector_lane, "count": len(cands),
            "candidates": cands, "hub": vs.hub_get(sid)}


def graph_read(body: GraphReadRequest) -> dict:
    from app.graph_db import get_project_graph

    sid = _source(body.source_id)
    ids = _ids(body.core_ids)
    if not ids:
        raise HTTPException(400, "core_ids must name at least one node")
    nodes, edges = get_project_graph(sid).fetch_graph(ids, config.HIERARCHY_EDGES, body.hops)
    found = {n.get("core_id") for n in nodes}
    return {"source_id": sid, "core_ids": ids, "hops": body.hops, "missing": [i for i in ids if i not in found],
            "count": {"nodes": len(nodes), "edges": len(edges)}, "nodes": nodes, "edges": edges}


def graph_traverse(body: GraphTraverseRequest) -> dict:
    from app.graph_db import get_project_graph

    sid = _source(body.source_id)
    ids = _ids(body.start_ids)
    if not ids:
        raise HTTPException(400, "start_ids must name at least one node")
    rels = [r for r in (_relation(x) for x in (body.relations or [])) if r] or None
    edges = get_project_graph(sid).traverse_logic(ids, rels, body.max_depth)
    reached = list(dict.fromkeys(e["target_core_id"] for e in edges if e["target_core_id"] not in ids))
    return {"source_id": sid, "start_ids": ids, "relations": rels, "max_depth": body.max_depth,
            "count": {"edges": len(edges), "reached": len(reached)}, "reached": reached, "edges": edges}


def attach(app: FastAPI) -> None:
    @app.get("/orchestrator/project_match", tags=["orchestrator"])
    def orchestrator_project_match(text: str = ""):
        return project_match(text)

    @app.post("/orchestrator/hybrid", tags=["orchestrator"])
    def orchestrator_hybrid(body: HybridRequest, request: Request):
        return hybrid(request, body)

    @app.post("/orchestrator/graph_read", tags=["orchestrator"])
    def orchestrator_graph_read(body: GraphReadRequest):
        return graph_read(body)

    @app.post("/orchestrator/graph_traverse", tags=["orchestrator"])
    def orchestrator_graph_traverse(body: GraphTraverseRequest):
        return graph_traverse(body)
