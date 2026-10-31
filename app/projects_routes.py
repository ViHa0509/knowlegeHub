"""Project endpoints (spec §2.8): /projects, /projects/registry[/sync], /projects/{id}[/visibility].

Create, delete and visibility go to the identity service (KC) first with the caller's own token (sign-in on);
KH then keeps the registry row, bundle naming, C4 registry row and the visibility mirror.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from app import auth, personal, project_access, projects, sqldb
from app import projects_store as store
from app import vector_store as vs

log = logging.getLogger(__name__)

C4_FAILED = "failed - run POST /projects/registry/sync"


def _encoder(request: Request):
    model = getattr(request.app.state, "model", None)
    if model is None:
        return None
    return lambda texts: model.encode(texts, normalize_embeddings=True)


def _kc(method: str, path: str, body: dict | None, passed: tuple[int, ...], tolerated: tuple[int, ...] = ()):
    """Relay to KC. Returns the status; raises HTTPException with the KC refusal (passed statuses) or 502."""
    try:
        status, data = auth.relay(method, path, body)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"identity service unavailable: {e}")
    if 200 <= status < 300 or status in tolerated:
        return status
    if status in passed:
        if isinstance(data, dict) and (data.get("account_required") or data.get("ask")):
            raise HTTPException(status_code=status, detail=data)
        raise HTTPException(status_code=status, detail=f"identity service refused: {auth.kc_error(data)}")
    raise HTTPException(status_code=502, detail=f"identity service answered {status}: {auth.kc_error(data)}")


def _with_vis(entry: dict) -> dict:
    out = dict(entry)
    out["visibility"] = project_access.visibility(entry["id"])
    return out


class ProjectCreate(BaseModel):
    project_id: Optional[str] = None
    id: Optional[str] = None
    role: Optional[str] = None
    aliases: list[str] = []
    keywords: list[str] = []
    tools: list[str] = []
    descriptor: Optional[str] = ""
    conflicts_with: list[str] = []
    source: Optional[str] = None
    visibility: Optional[str] = None


class VisibilityBody(BaseModel):
    visibility: Any = None


def list_route(source: Optional[str] = None) -> dict:
    entries = projects.list_projects()
    visible = auth.visible_projects("project.read")
    out = {}
    for pid in sorted(entries):
        e = entries[pid]
        if visible is not None and pid not in visible:
            continue
        if source and (e.get("source") or "KH") != source:
            continue
        out[pid] = _with_vis(e)
    return {"version": 2, "projects": out,
            "store": {"db": sqldb.backend(), "table": store.TABLE, "projects": len(entries)}}


def registry_route() -> dict:
    h = vs.get_existing(vs.part_collection_name("knowledge", projects.ORCHESTRATOR))
    rows = []
    if h is not None:
        for rid, meta in vs.meta_rows_vt(h, {"kind": {"$eq": "project-registry"}}):
            if not auth.row_visible(meta, rid):
                continue
            rows.append({"id": meta.get("project_id") or rid[len(projects.REGISTRY_PREFIX):],
                         "role": meta.get("role") or "project",
                         "aliases": projects._split_pipe(meta.get("aliases")),
                         "keywords": projects._split_pipe(meta.get("keywords")),
                         "descriptor": meta.get("descriptor") or "", "source": "c4"})
    rows.sort(key=lambda r: r["id"])
    return {"count": len(rows), "source": "c4" if rows else "empty", "projects": rows}


def create_route(body: ProjectCreate, encode=None) -> dict:
    pid = (body.project_id or body.id or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="project_id is required")
    if body.source is not None and len(str(body.source)) > 64:
        raise HTTPException(status_code=400, detail="source must be at most 64 characters")
    p = auth.current()
    signed_in = p is not None
    if signed_in and personal.is_personal(pid):
        raise HTTPException(status_code=403,
                            detail=f"personal projects are created by KH itself; yours is {p.personal_project}")
    try:
        pid = projects.validate_id(pid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    vis = body.visibility
    if vis is not None and vis not in project_access.VISIBILITIES:
        raise HTTPException(status_code=400,
                            detail=f"visibility must be one of {', '.join(project_access.VISIBILITIES)}")
    existing = projects.exists(pid)
    if existing:
        if not auth.may(pid, "project.write"):
            raise HTTPException(status_code=403, detail="missing permission project.write on this project")
    kc_registered = False
    if not existing and signed_in and not personal.is_personal(pid):
        _kc("POST", "/projects", {"id": pid, "visibility": vis or project_access.PRIVATE}, passed=(400, 403, 409))
        kc_registered = True
    try:
        entry = projects.get_or_create_project(pid, role=body.role, aliases=body.aliases, keywords=body.keywords,
                                               tools=body.tools, descriptor=body.descriptor or "",
                                               conflicts_with=body.conflicts_with, source=body.source)
    except ValueError as e:
        if kc_registered:
            try:
                auth.relay("DELETE", f"/projects/{pid}", None)
            except Exception as e2:  # noqa: BLE001
                log.warning("withdrawing KC registration of %s failed: %s", pid, e2)
        raise HTTPException(status_code=400, detail=str(e))
    if not existing and vis and vis != project_access.PRIVATE and not project_access.fixed(pid):
        try:
            project_access.set_visibility(pid, vis, getattr(p, "email", None))
        except Exception as e:  # noqa: BLE001
            log.warning("visibility mirror for %s failed: %s", pid, e)
    out = _with_vis(entry)
    if not existing and signed_in:
        out["token_refresh_needed"] = True
    try:
        projects.sync_orchestrator_registry(encode)
        out["c4_registry"] = "written"
    except Exception as e:  # noqa: BLE001
        log.warning("C4 registry row for %s failed: %s", pid, e)
        out["c4_registry"] = C4_FAILED
    return out


def get_route(project_id: str) -> dict:
    e = projects.get_project(project_id)
    if e is None:
        raise HTTPException(status_code=404, detail=f"unknown project '{project_id}'")
    out = _with_vis(e)
    out["counter"] = projects.read_counter(project_id)
    return out


def visibility_route(project_id: str, vis: Any) -> dict:
    if not projects.exists(project_id):
        raise HTTPException(status_code=404, detail=f"unknown project '{project_id}'")
    if vis not in project_access.VISIBILITIES:
        raise HTTPException(status_code=400,
                            detail=f"visibility must be one of {', '.join(project_access.VISIBILITIES)}")
    if project_access.fixed(project_id):
        raise HTTPException(status_code=400, detail=f"the visibility of '{project_id}' is fixed (private)")
    p = auth.current()
    if p is not None:
        _kc("PUT", f"/projects/{project_id}/visibility", {"visibility": vis}, passed=(400, 403, 404))
    project_access.set_visibility(project_id, vis, getattr(p, "email", None))
    return {"project": project_id, "visibility": vis, "token_refresh_needed": True}


def delete_route(project_id: str, confirm: bool) -> dict:
    if not confirm:
        raise HTTPException(status_code=400, detail="deleting a project needs ?confirm=true")
    if project_id == projects.ORCHESTRATOR:
        raise HTTPException(status_code=400, detail="the orchestrator project cannot be deleted")
    if auth.current() is not None and not personal.is_personal(project_id):
        _kc("DELETE", f"/projects/{project_id}", None, passed=(400, 403), tolerated=(404,))
    try:
        return projects.delete_project(project_id, confirm=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def attach(app: FastAPI) -> None:
    @app.get("/projects", tags=["projects"])
    def projects_list(source: Optional[str] = None):
        return list_route(source)

    @app.post("/projects", tags=["projects"])
    def projects_create(body: ProjectCreate, request: Request):
        return create_route(body, _encoder(request))

    @app.get("/projects/registry", tags=["projects"])
    def projects_registry():
        return registry_route()

    @app.post("/projects/registry/sync", tags=["projects"])
    def projects_registry_sync(request: Request):
        return projects.sync_orchestrator_registry(_encoder(request))

    @app.get("/projects/{project_id}", tags=["projects"])
    def projects_get(project_id: str):
        return get_route(project_id)

    @app.put("/projects/{project_id}/visibility", tags=["projects"])
    def projects_visibility(project_id: str, body: VisibilityBody):
        return visibility_route(project_id, body.visibility)

    @app.delete("/projects/{project_id}", tags=["projects"])
    def projects_delete(project_id: str, confirm: bool = False):
        return delete_route(project_id, confirm)
