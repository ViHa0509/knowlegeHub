"""Space routes of KH 7.1 (read side): /spaces, /spaces/match, scopes, access, rows, index, search, map, walk,
plus /projects/{id}/layout and /projects/{id}/about.

Shapes follow the KH 7.1.0 build (captures in docs/ref71/). Every space is served in the legacy frame: the
rows stay in the v6.5 collections (app/spaces/frame.py).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import config
from app import ctx_util as U
from app import vector_store as vs
from app.spaces import frame as F
from app.spaces import mapper as M

_LAYOUT_SEED = Path(config.ROOT) / "seeds" / "project_layout.json"


def _need_space(space: str) -> str:
    from app import projects

    sid = vs.normalize_project(space) or ""
    if not sid or not (projects.exists(sid) or vs.project_collections(sid) or sid == config.ORCHESTRATOR_ID):
        raise HTTPException(status_code=404, detail=f"no such space ('{space}')")
    if not U.may(sid, "project.read"):
        raise HTTPException(status_code=403, detail="missing permission project.read on this space")
    return sid


def _need_scope(space: str, scope: str) -> dict:
    s = F.scope_of(scope)
    if not s:
        raise HTTPException(status_code=404, detail=f'no such scope ("{space}/{scope}")')
    return s


def list_spaces() -> dict:
    from app import project_access, projects

    entries, _src = projects.registry_entries()
    items = []
    for e in entries:
        pid = e["id"]
        if not U.may(pid, "project.read"):
            continue
        role = e.get("role") or "project"
        typ = "orchestrator" if pid == config.ORCHESTRATOR_ID else ("project" if role == "project" else role)
        if pid != config.ORCHESTRATOR_ID and e.get("source") in ("sync", "legacy"):
            typ = "legacy"
        try:
            vis = project_access.visibility(pid)
        except Exception:  # noqa: BLE001
            vis = "private"
        items.append({"id": pid, "name": pid, "type": typ, "desc": e.get("descriptor") or "", "vis": vis or "private",
                      "recent": False})
    return {"total": len(items), "items": items}


def match(text: str) -> dict:
    from app import orchestrator

    m = orchestrator.project_match(text or "")
    items = [{"space": h["project"], "term": h["matched_term"], "on": h["matched_on"]} for h in m["matches"]]
    return {"best_space": m["best"], "ambiguous": m["ambiguous"], "weak_only": m["weak_only"], "items": items}


def scopes(space: str) -> dict:
    out = []
    for s in F.SCOPES:
        row = {"scope": s["scope"], "store": s["store"], "backing": F.backing(space, s), "system": s["system"],
               "rows": F.count(space, s) if not s["system"] else 0}
        if s["alias"]:
            row["alias"] = s["alias"]
        out.append(row)
    return {"space": space, "scopes": out}


def access(space: str) -> dict:
    write = U.may(space, "project.write")
    relation = "owner" if write else "reader"
    why = f"{relation} (legacy frame: the token's rights)"
    sc = {}
    for s in F.SCOPES:
        if s["system"]:
            continue
        sc[s["scope"]] = {"read": True, "write": bool(write), "why": why}
    return {"space": space, "relation": relation, "frame": "legacy", "scopes": sc}


def scope_rows(space: str, scope: str, limit: int, offset: int) -> dict:
    s = _need_scope(space, scope)
    if s["scope"] == "about":
        a = F.about_row(space)
        items = [F.row_out(space, s, a)] if a else []
        return {"space": space, "scope": s["scope"], "store": s["store"], "truncated": False, "total": len(items),
                "total_capped": False, "offset": 0, "has_more": False, "next_offset": None, "items": items}
    rows: list[dict] = []
    for h, where in F.lanes(space, s):
        try:
            got = vs.get_vt(h, where=where)
        except Exception:  # noqa: BLE001
            continue
        rows.extend(F.row_out(space, s, g) for g in got if not g["metadata"].get("chunk_of"))
    rows.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
    total = len(rows)
    page = rows[offset:offset + limit]
    more = offset + limit < total
    return {"space": space, "scope": s["scope"], "store": s["store"], "truncated": False, "total": total,
            "total_capped": False, "offset": offset, "has_more": more, "next_offset": offset + limit if more else None,
            "items": page}


def open_row(space: str, row: str, section: Optional[str] = None) -> dict:
    if row.strip() == "~more":
        page = M.take_more(space)
        lines = [M._line_flat(r) for r in page]
        return {"id": "~more", "space": space, "count": len(page),
                "text": "\n".join(lines) if lines else "nothing more: the map's rows are all shown", "items":
                [r["qid"] for r in page]}
    sp, sc, rid = F.split_qid(row)
    if sp and sp != space:
        space = _need_space(sp)
    r = F.find_row(space, rid, sc)
    if r is None:
        raise HTTPException(status_code=404, detail=f"no row '{row}' in what you can read of space '{space}'")
    return F.expand_out(r, section)


def walk(space: str, start: str, depth: int, kinds: Optional[str]) -> dict:
    want = {k.strip() for k in (kinds or "").split(",") if k.strip()}
    sp, sc, rid = F.split_qid(start)
    space = sp or space
    first = F.find_row(space, rid, sc)
    if first is None:
        raise HTTPException(status_code=404, detail=f"no row '{start}' in what you can read of space '{space}'")
    seen = {first["qid"]}
    frontier = [(first, 0)]
    items = []
    while frontier:
        cur, d = frontier.pop(0)
        if d >= max(1, min(depth, 2)):
            continue
        for ln in cur.get("links") or []:
            if not isinstance(ln, dict):
                continue
            kind, to = ln.get("kind") or "", ln.get("to") or ""
            if want and kind not in want:
                continue
            tsp, tsc, tid = F.split_qid(to)
            tsp = tsp or space
            if not U.may(tsp, "project.read"):
                continue
            nxt = F.find_row(tsp, tid, tsc)
            if nxt is None or nxt["qid"] in seen:
                continue
            seen.add(nxt["qid"])
            items.append({"from": cur["qid"], "kind": kind, "to": nxt["qid"], "hop": d + 1, "name": nxt["name"],
                          "summary": nxt["summary"]})
            frontier.append((nxt, d + 1))
    return {"from": first["qid"], "depth": depth, "count": len(items), "items": items}


def layout(project: str) -> dict:
    try:
        d = json.loads(_LAYOUT_SEED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {"layout": "legacy", "kind": "project", "scopes": []}
    d["project"] = project
    a = F.about_row(project)
    d["about"] = (a.get("source_text") if a else None)
    if project == config.ORCHESTRATOR_ID:
        d["kind"] = "orchestrator"
    elif vs.is_personal(project):
        d["kind"] = "personal"
    return d


def about_of(project: str) -> dict:
    a = F.about_row(project)
    if not a:
        raise HTTPException(status_code=404, detail=f"project '{project}' has no About yet")
    return {"project": project, "id": a["vector_id"], "text": a.get("source_text") or "",
            "updated_at": (a.get("metadata") or {}).get("ts")}


def put_about(project: str, text: str, model) -> dict:
    if not U.may(project, "project.write"):
        raise HTTPException(status_code=403, detail="missing permission project.write on this project")
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")
    coll = vs.get_or_create(vs.part_collection_name("about", project))
    rid = f"about__{project}"
    from app.projects_store import now_iso

    meta = {"project": project, "scope": "about", "kind": "about", "name": f"About {project}",
            "ts": now_iso()}
    from app.embeddings import get_embedder

    vec = (model or get_embedder()).encode(text, normalize_embeddings=True)
    vs.upsert_one(coll, rid, vec, text, meta)
    return {"project": project, "id": rid, "saved": True}


class MapBody(BaseModel):
    text: str = Field(..., min_length=1)
    max_chars: Optional[int] = Field(None, ge=500, le=20000)


class SearchBody(BaseModel):
    text: str = Field(..., min_length=1)
    k: int = Field(8, ge=1, le=50)
    min_score: float = 0.0


class AboutBody(BaseModel):
    text: str


def attach(app: FastAPI) -> None:
    @app.get("/spaces", tags=["spaces"])
    def spaces_list():
        return list_spaces()

    @app.get("/spaces/match", tags=["spaces"])
    def spaces_match(text: str = ""):
        return match(text)

    @app.get("/spaces/{space}/scopes", tags=["spaces"])
    def spaces_scopes(space: str):
        return scopes(_need_space(space))

    @app.get("/spaces/{space}/access", tags=["spaces"])
    def spaces_access(space: str):
        return access(_need_space(space))

    @app.get("/spaces/{space}/index", tags=["spaces"])
    def spaces_index(space: str, key: str = Query(..., min_length=1)):
        sid = _need_space(space)
        return {"space": sid, "key": key, "items": M.index_lookup(sid, key)}

    @app.get("/spaces/{space}/scopes/{scope}/rows", tags=["spaces"])
    def spaces_scope_rows(space: str, scope: str, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        return scope_rows(_need_space(space), scope, limit, offset)

    @app.get("/spaces/{space}/scopes/{scope}/rows/{row}", tags=["spaces"])
    def spaces_scope_row(space: str, scope: str, row: str):
        sid = _need_space(space)
        s = _need_scope(sid, scope)
        r = F.find_row(sid, row, s["scope"])
        if r is None:
            raise HTTPException(status_code=404, detail=f"no row '{row}' in {sid}/{s['scope']}")
        return r

    @app.post("/spaces/{space}/scopes/{scope}/search", tags=["spaces"])
    async def spaces_scope_search(request: Request, space: str, scope: str, body: SearchBody):
        sid = _need_space(space)
        s = _need_scope(sid, scope)
        rows, searched, _b = await asyncio.to_thread(M.meaning, sid, body.text, U.model_of(request), None,
                                                     (s["scope"],), body.k)
        rows = [r for r in rows if r["score"] >= body.min_score][:body.k]
        return {"space": sid, "scope": s["scope"], "searched": searched, "count": len(rows), "items": rows}

    @app.get("/spaces/{space}/rows/{row:path}", tags=["spaces"])
    def spaces_row(space: str, row: str, section: Optional[str] = None):
        return open_row(_need_space(space), row, section)

    @app.post("/spaces/{space}/map", tags=["spaces"])
    async def spaces_map(request: Request, space: str, body: MapBody):
        sid = _need_space(space)
        return await asyncio.to_thread(M.build_map, sid, body.text, U.model_of(request),
                                       body.max_chars)

    @app.get("/spaces/{space}/links/walk", tags=["spaces"])
    def spaces_walk(space: str, start: str = Query(..., alias="from"), depth: int = Query(1, ge=1, le=2),
                    links: Optional[str] = None):
        return walk(_need_space(space), start, depth, links)

    @app.get("/spaces/{space}/gaps", tags=["spaces"])
    def spaces_gaps(space: str, open: bool = True, limit: int = Query(50, ge=1, le=500)):
        return {"space": _need_space(space), "open": open, "total": 0, "items": []}

    @app.get("/spaces/{space}/links/learned", tags=["spaces"])
    def spaces_links_learned(space: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500)):
        return {"space": _need_space(space), "total": 0, "offset": offset, "limit": limit, "items": []}

    @app.get("/projects/{project_id}/layout", tags=["projects"])
    def project_layout(project_id: str):
        return layout(_need_space(project_id))

    @app.get("/projects/{project_id}/about", tags=["projects"])
    def project_about(project_id: str):
        return about_of(_need_space(project_id))

    @app.put("/projects/{project_id}/about", tags=["projects"])
    async def project_about_put(request: Request, project_id: str, body: AboutBody):
        sid = _need_space(project_id)
        return await asyncio.to_thread(put_about, sid, body.text, U.model_of(request))
