"""Semantic policy tier (spec §3.7.1), LEGACY: live routes, but nothing routes on them since 2026-08-21.

Collections kh_policy_<project>; kh_policy_orchestrator is the common tier. Embedded text = intent + " " + tags.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from app import ctx_config as C
from app import ctx_util as U

log = logging.getLogger(__name__)

POLICY_MATCH_THRESHOLD = C.POLICY_MATCH_THRESHOLD
ORCH = "orchestrator"


def _coll(project: str, create: bool):
    from app import vector_store as vs

    name = vs.part_collection_name("policy", project)
    return vs.get_or_create(name) if create else vs.get_existing(name)


class PolicyAdd(BaseModel):
    intent: str = Field(..., min_length=1, max_length=2000)
    body: str = Field(..., min_length=1)
    project: Optional[str] = None
    priority: int = 0
    tags: list[str] | str | None = None
    policy_id: Optional[str] = None


class PolicyMatch(BaseModel):
    intent: str = Field(..., min_length=1)
    project: Optional[str] = None
    k: int = Field(10, ge=1, le=100)
    threshold: Optional[float] = None


class PolicyDelete(BaseModel):
    policy_id: str
    project: Optional[str] = None


def _tags_csv(tags) -> str:
    if tags is None:
        return ""
    if isinstance(tags, str):
        tags = tags.split(",")
    return ",".join(t.strip() for t in tags if t and t.strip())


def add(body: PolicyAdd, model) -> dict:
    from app import vector_store as vs

    pid = vs.own_project(body.project)
    scope = "common" if pid == ORCH else "project"
    tags = _tags_csv(body.tags)
    policy_id = body.policy_id or f"pol_{pid}_{os.urandom(5).hex()}"
    text = (body.intent + " " + tags).strip()
    meta = {"policy_id": policy_id, "project": pid, "scope": scope, "intent": body.intent, "body": body.body,
            "priority": int(body.priority), "tags": tags, "updated_at": U.now_iso()}
    vec = model.encode(text, normalize_embeddings=True)
    vs.upsert_one(_coll(pid, True), policy_id, vec, text, meta)
    return {"status": "ok", "policy_id": policy_id, "project": pid, "scope": scope}


def match(body: PolicyMatch, model) -> dict:
    from app import vector_store as vs

    thr = POLICY_MATCH_THRESHOLD if body.threshold is None else float(body.threshold)
    pid = vs.normalize_project(body.project)
    lanes = []
    if pid and pid != ORCH:
        lanes.append(_coll(pid, False))
    lanes.append(_coll(ORCH, False))
    vec = model.encode(body.intent, normalize_embeddings=True)
    fired = []
    for h in lanes:
        if h is None:
            continue
        for hit in vs.query_vt(h, vec, body.k):
            if hit["score"] < thr:
                continue
            m = hit["metadata"] or {}
            prio = int(m.get("priority") or 0)
            eff = hit["score"] + (0.03 if m.get("scope") == "project" else 0.0) + min(prio, 20) * 0.005
            fired.append({"policy_id": m.get("policy_id") or hit["vector_id"], "project": m.get("project"),
                          "scope": m.get("scope"), "intent": m.get("intent"), "body": m.get("body"),
                          "priority": prio, "tags": m.get("tags") or "", "score": round(hit["score"], 4),
                          "effective": round(eff, 4)})
    fired.sort(key=lambda f: -f["effective"])
    return {"winner": fired[0] if fired else None, "fired": fired, "count": len(fired), "threshold": thr}


def attach(app: FastAPI) -> None:
    @app.post("/policy/add", tags=["policy"])
    def policy_add(body: PolicyAdd, request: Request):
        U.need(body.project or ORCH, "project.write")
        return add(body, U.model_of(request))

    @app.get("/policy/list", tags=["policy"])
    def policy_list(project: Optional[str] = ORCH):
        from app import vector_store as vs

        pid = vs.own_project(project)
        U.need(pid, "project.read")
        h = _coll(pid, False)
        rows = [] if h is None else vs.get_vt(h)
        out = [{"policy_id": r["vector_id"], **{k: v for k, v in (r["metadata"] or {}).items()
                                                  if k in ("project", "scope", "intent", "body", "priority", "tags",
                                                           "updated_at")}} for r in rows]
        return {"project": pid, "count": len(out), "policies": out}

    @app.post("/policy/match", tags=["policy"])
    def policy_match(body: PolicyMatch, request: Request):
        U.need(body.project or ORCH, "project.read")
        return match(body, U.model_of(request))

    @app.delete("/policy", tags=["policy"])
    def policy_delete(body: dict = Body(...)):
        # The original takes a raw JSON object here: {policy_id, project?}.
        from app import vector_store as vs

        try:
            body = PolicyDelete.model_validate(body)
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=e.errors(include_url=False, include_context=False))
        pid = vs.own_project(body.project)
        U.need(pid, "project.write")
        h = _coll(pid, False)
        if h is None:
            raise HTTPException(status_code=404, detail="no policy collection for this project")
        deleted = vs.delete_vt([body.policy_id], h)
        return {"deleted": len(deleted), "policy_id": body.policy_id}
