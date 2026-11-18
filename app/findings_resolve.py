"""POST /findings/resolve {project, id, solution}: a solved finding becomes a lesson (KH 7.x).

Findings hold open problems only (kernel rule 4). Resolving one writes a lesson row (scope lesson of the
same project, or the personal lesson scope of a personal project) with the finding's text and its solution,
then removes the finding. The lesson keeps the finding id in `from_finding`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from app import config
from app import ctx_util as U
from app import vector_store as vs


class ResolveBody(BaseModel):
    project: Optional[str] = None
    id: Optional[str] = None
    solution: Optional[str] = None


def _finding(project: str, fid: str) -> tuple[Optional[object], Optional[dict]]:
    for h in vs.read_collections("findings", project):
        got = vs.get_ids_vt(h, [fid])
        if got:
            return h, got[0]
    return None, None


def resolve(body: ResolveBody, model) -> dict:
    project = vs.normalize_project(body.project)
    fid = (body.id or "").strip()
    if not project or not fid:
        raise HTTPException(status_code=400, detail="project and id required")
    solution = (body.solution or "").strip()
    if not solution:
        raise HTTPException(status_code=400,
                            detail="solution required: a finding without a concrete solution stays a finding")
    if not U.may(project, "project.write"):
        raise HTTPException(status_code=403, detail="missing permission project.write on this project")
    h, row = _finding(project, fid)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no finding '{fid}' in project '{project}'")
    meta_f = dict(row.get("metadata") or {})
    problem = (row.get("source_text") or "").strip()
    text = f"{problem}\nSOLUTION: {solution}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lesson_id = "les_" + uuid.uuid4().hex[:12]
    meta = {"project": project, "scope": config.PERSONAL_LESSON_SCOPE, "kind": "lesson", "from_finding": fid,
            "ts": now, "solution": solution[:2000]}
    for k in ("topic", "name", "subject", "keys", "where"):
        if k in meta_f:
            meta[k] = meta_f[k]
    who = None
    try:
        from app import auth

        p = auth.current()
        who = getattr(p, "email", None)
    except Exception:  # noqa: BLE001
        pass
    if who:
        meta["author"] = who
    from app.embeddings import get_embedder

    vec = (model or get_embedder()).encode(text, normalize_embeddings=True)
    coll = vs.scope_collection(config.PERSONAL_LESSON_SCOPE, project)
    vs.upsert_one(coll, lesson_id, vec, text, meta)
    try:
        from app import chunks

        chunks.write_for(lesson_id, text, meta, who, model)
    except Exception:  # noqa: BLE001
        pass
    vs.delete_vt([fid], h)
    try:
        from app import chunks

        chunks.delete_for([fid])
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.eventing.emit import emit

        emit("kh.finding.resolved", {"finding": fid, "lesson": lesson_id}, project=project, email=who)
    except Exception:  # noqa: BLE001
        pass
    return {"project": project, "finding": fid, "lesson_id": lesson_id,
            "lesson": f"{project}/lesson-learns/{lesson_id}", "resolved_at": now}


def attach(app: FastAPI) -> None:
    @app.post("/findings/resolve", tags=["findings"])
    def findings_resolve(request: Request, body: ResolveBody):
        return resolve(body, U.model_of(request))

    attach_guard_tools(app)


def guard_tools(project: Optional[str] = None) -> dict:
    """GET /personalization/guard_tools: the tool names any rule the caller sees guards (kh-helper builds its
    PreToolUse matcher from them)."""
    from app import personalization
    from app.kernel import rules as R

    personal = U.personal_tier()
    proj = project or personalization._bound_project()
    if proj and not U.may(proj, "project.read"):
        proj = None
    tools: set[str] = set()
    try:
        for e in R.resolve(proj, personal):
            g = e["rule"].get("guard")
            if g and g.get("tool"):
                tools.add(str(g["tool"]))
    except Exception:  # noqa: BLE001
        pass
    if personal:
        h = vs.get_existing(vs.part_collection_name(config.PERSONAL_SCOPE, personal))
        if h is not None:
            for _rid, m in vs.meta_rows_vt(h):
                if m.get("guard_tool"):
                    tools.add(str(m["guard_tool"]))
    return {"tools": sorted(tools)}


def attach_guard_tools(app: FastAPI) -> None:
    @app.get("/personalization/guard_tools", tags=["kernel"])
    def personalization_guard_tools(project: Optional[str] = None):
        return guard_tools(project)
