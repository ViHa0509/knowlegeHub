"""Routing by meaning (spec §2.10): `/orchestrator/resolve`, shortcuts, facts, switch.

Collection `kh_orch_resolve` holds maintainer-written rows: `route-shortcut` (a phrase that should send a session
to a project, id `shortcut__<project>` unless named) and `fact` (a common answer).

`POST /orchestrator/resolve {query|intent, session_source?, k=5, threshold=0.45, route_threshold=0.55,
semantic_policy?}` decides, in order: a LEGACY semantic policy winner naming a project (`policy`, only with
`semantic_policy`), the first `fact` hit (`answer`), the first `route-shortcut` at or above route_threshold
(`route` with `switch_to`, `binding`, `metadata`), else `answer_common`. A route to a project the caller cannot
read becomes `answer_common`, and candidates of unreadable projects are stripped.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app import auth, projects
from app import vector_store as vs

log = logging.getLogger(__name__)

COLLECTION = "kh_orch_resolve"
KIND_SHORTCUT = "route-shortcut"
KIND_FACT = "fact"


class ResolveBody(BaseModel):
    query: Optional[str] = None
    intent: Optional[str] = None
    session_source: Optional[str] = None
    k: int = Field(5, ge=1, le=50)
    threshold: float = 0.45
    route_threshold: float = 0.55
    semantic_policy: Optional[bool] = None


class ShortcutBody(BaseModel):
    project_id: Optional[str] = None
    project: Optional[str] = None
    text: str = Field(..., min_length=1)
    shortcut_id: Optional[str] = None


class FactBody(BaseModel):
    text: str = Field(..., min_length=1)
    subject: Optional[str] = None
    fact_id: Optional[str] = None


def _encode(request: Request, text: str):
    model = getattr(request.app.state, "model", None)
    if model is None:
        from app.embeddings import get_embedder

        model = get_embedder()
    return model.encode(text, normalize_embeddings=True)


def binding_of(pid: str) -> dict:
    b = projects.bundle_of(pid)
    return {"project_id": pid, "vector": b["vector"], "knowledge": b["knowledge"], "graph": b["graph_dir"],
            "policy": b["policy"], "findings": b["findings"], "log": b["log"]}


def _policy_winner(vec, k: int, threshold: float) -> dict | None:
    """LEGACY semantic policy tier (§3.7.1): best policy row naming a project, if any."""
    h = vs.get_existing(vs.part_collection_name("policy", projects.ORCHESTRATOR))
    if h is None:
        return None
    for hit in vs.query_vt(h, vec, k):
        meta = hit.get("metadata") or {}
        target = meta.get("route_to") or meta.get("project_id")
        if hit["score"] >= threshold and target:
            return {"vector_id": hit["vector_id"], "score": hit["score"], "project_id": target,
                    "text": hit.get("source_text") or ""}
    return None


def resolve(body: ResolveBody, vec) -> dict:
    query = (body.query or body.intent or "").strip()
    out: dict[str, Any] = {"query": query, "session_source": body.session_source, "decision": "answer_common"}
    candidates = []
    h = vs.get_existing(COLLECTION)
    if h is not None and h.count():
        for hit in vs.query_vt(h, vec, body.k):
            if hit["score"] < body.threshold:
                continue
            meta = hit.get("metadata") or {}
            candidates.append({"id": hit["vector_id"], "kind": meta.get("kind"), "score": hit["score"],
                               "project_id": meta.get("project_id"), "text": hit.get("source_text") or "",
                               "metadata": meta})
    # _visible_decision: strip candidates of projects the caller cannot read
    candidates = [c for c in candidates if not c.get("project_id") or auth.may(c["project_id"], "project.read")]
    if body.semantic_policy:
        pol = _policy_winner(vec, body.k, body.threshold)
        if pol is not None and auth.may(pol["project_id"], "project.read"):
            out.update({"decision": "policy", "policy": pol, "switch_to": pol["project_id"],
                        "binding": binding_of(pol["project_id"])})
    if out["decision"] == "answer_common":
        fact = next((c for c in candidates if c["kind"] == KIND_FACT), None)
        route = next((c for c in candidates if c["kind"] == KIND_SHORTCUT and c["score"] >= body.route_threshold),
                     None)
        if fact is not None:
            out.update({"decision": "answer", "answer": fact["text"], "metadata": fact["metadata"]})
        elif route is not None and route.get("project_id"):
            out.update({"decision": "route", "switch_to": route["project_id"],
                        "binding": binding_of(route["project_id"]), "metadata": route["metadata"]})
    out["candidates"] = [{k: v for k, v in c.items() if k != "metadata"} for c in candidates]
    return out


def attach(app: FastAPI) -> None:
    @app.post("/orchestrator/resolve", tags=["orchestrator"])
    def orchestrator_resolve(body: ResolveBody, request: Request):
        if not (body.query or body.intent or "").strip():
            raise HTTPException(status_code=422, detail="query or intent is required")
        if body.session_source and not auth.may(vs.normalize_project(body.session_source), "project.read"):
            raise HTTPException(status_code=403, detail="missing permission project.read on this project")
        return resolve(body, _encode(request, (body.query or body.intent).strip()))

    @app.post("/orchestrator/shortcut", tags=["orchestrator"])
    def orchestrator_shortcut(body: ShortcutBody, request: Request):
        pid = (body.project_id or body.project or "").strip()
        if not pid:
            raise HTTPException(status_code=422, detail="project_id is required")
        if not projects.exists(pid):
            raise HTTPException(status_code=404, detail=f"unknown project '{pid}'")
        rid = (body.shortcut_id or "").strip() or f"shortcut__{pid}"
        meta = {"kind": KIND_SHORTCUT, "project_id": pid, "project": projects.ORCHESTRATOR}
        vs.upsert_one(vs.get_or_create(COLLECTION), rid, _encode(request, body.text), body.text, meta)
        return {"status": "ok", "id": rid, "project_id": pid, "kind": KIND_SHORTCUT}

    @app.post("/orchestrator/fact", tags=["orchestrator"])
    def orchestrator_fact(body: FactBody, request: Request):
        rid = (body.fact_id or "").strip() or "fact__" + hashlib.sha256(body.text.encode()).hexdigest()[:12]
        meta = {"kind": KIND_FACT, "project": projects.ORCHESTRATOR}
        if body.subject:
            meta["subject"] = body.subject
        vs.upsert_one(vs.get_or_create(COLLECTION), rid, _encode(request, body.text), body.text, meta)
        return {"status": "ok", "id": rid, "kind": KIND_FACT}

    @app.get("/orchestrator/switch/{project_id}", tags=["orchestrator"])
    def orchestrator_switch(project_id: str):
        pid = vs.normalize_project(project_id) or project_id
        if not projects.exists(pid):
            raise HTTPException(status_code=404, detail=f"unknown project '{project_id}'")
        return {"session_source": pid, "binding": binding_of(pid)}
