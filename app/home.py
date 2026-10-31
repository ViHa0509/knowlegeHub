"""/home admin page (spec §4.10): `GET /home` serves `app/static/home.html` (public, no-store); sign-in proxy
(device code + PKCE S256 against Keycloak); Knowledge tab (projects, items, parts, members, personal backfill);
Lesson learn tab (lessons; the learn-back box is `app/learnback.py`); Sync tab uses `/sync/*` (`app/sync.py`).

Test seam: `HTTP` (requests-like session) replaces calls to Keycloak.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import uuid
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app import config
from app import vector_store as vs

log = logging.getLogger(__name__)

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "home.html")
PARTS = ("knowledge", "log", "findings", "entity", "chunk", "policy", "src")
BIG_PART = 20000
HTTP: Any = None


def _issuer() -> str | None:
    from app import auth

    fn = getattr(auth, "issuer", None)
    return (fn() if callable(fn) else None) or config._env("KH_AUTH_ISSUER")


def _client_id() -> str:
    return config._env("KH_AUTH_CLIENT_ID", "kh-cli") or "kh-cli"


def _http():
    if HTTP is not None:
        return HTTP
    import requests

    return requests


def _email() -> str | None:
    from app import auth

    p = auth.current()
    return getattr(p, "email", None) if p is not None else None


# ---------------------------------------------------------------------------
# Sign-in proxy
# ---------------------------------------------------------------------------


def _need_issuer() -> str:
    iss = _issuer()
    if not iss:
        raise HTTPException(status_code=409, detail="sign-in is off on this KH")
    return iss.rstrip("/")


def login_start() -> dict:
    iss = _need_issuer()
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    r = _http().post(f"{iss}/protocol/openid-connect/auth/device",
                     data={"client_id": _client_id(), "scope": "openid", "code_challenge": challenge,
                           "code_challenge_method": "S256"}, timeout=15)
    if r.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"identity service answered {r.status_code}")
    d = r.json()
    return {"device_code": d.get("device_code"), "user_code": d.get("user_code"),
            "verification_uri": d.get("verification_uri"),
            "verification_uri_complete": d.get("verification_uri_complete"), "expires_in": d.get("expires_in"),
            "interval": d.get("interval", 5), "code_verifier": verifier}


def _token(data: dict) -> dict:
    iss = _need_issuer()
    r = _http().post(f"{iss}/protocol/openid-connect/token", data={"client_id": _client_id(), **data}, timeout=15)
    try:
        d = r.json()
    except ValueError:
        d = {}
    if r.status_code < 300 and d.get("access_token"):
        return {"state": "ok", "access_token": d["access_token"], "refresh_token": d.get("refresh_token"),
                "expires_in": d.get("expires_in")}
    if d.get("error") in ("authorization_pending", "slow_down"):
        return {"state": "pending"}
    return {"state": "failed", "error": d.get("error") or f"status {r.status_code}"}


# ---------------------------------------------------------------------------
# Knowledge tab
# ---------------------------------------------------------------------------


def _part_handles(project: str, part: str):
    from app import sync

    return sync.part_collections(project, part)


def _graph_size(pid: str):
    if config.GRAPH_BACKEND == "hana":
        return "hana"
    d = os.path.join(config.KUZU_PROJECTS_PATH, pid)
    total = 0
    for root, _dirs, files in os.walk(d):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def projects_overview() -> list[dict]:
    from app import projects, sync

    out = []
    for pid, e in sorted(projects.list_projects().items()):
        out.append({"id": pid, "descriptor": e.get("descriptor") or "", "personal": vs.is_personal(pid),
                    "role": e.get("role"), "visibility": e.get("visibility"),
                    "parts": {p: sync.part_count(pid, p) for p in PARTS}, "graph": _graph_size(pid)})
    return out


_META_KEYS = ("kind", "topic", "subject", "status", "author", "title", "scope", "category", "action", "context")


def items(project: str, part: str, offset: int = 0, limit: int = 50) -> dict:
    from app import hotlog

    if part not in PARTS:
        raise HTTPException(status_code=422, detail=f"part must be one of {', '.join(PARTS)}")
    limit = max(0, min(int(limit), 500))
    lanes = _part_handles(project, part)
    total = sum(len(h.get(where=w, include=[]).get("ids") or []) if w else int(h.count()) for h, w in lanes)
    rows: list[dict] = []
    sorted_ = total <= BIG_PART
    if sorted_:
        stubs = []
        for h, w in lanes:
            for rid, m in vs.meta_rows_vt(h, w):
                stubs.append((hotlog.parse_ts(m.get("ts") or m.get("created_at")), rid, h))
        stubs.sort(key=lambda x: (x[0], x[1]), reverse=True)
        page = stubs[offset:] if limit == 0 else stubs[offset:offset + limit]
        for _t, rid, h in page:
            rows += vs.get_ids_vt(h, [rid])
    else:
        skip, want = offset, (limit or BIG_PART)
        for h, w in lanes:
            got = vs.get_vt(h, where=w, limit=want, offset=skip) if not w else vs.get_vt(h, where=w)[skip:skip + want]
            rows += got
            skip = max(0, skip - (int(h.count()) if not w else len(got)))
            want -= len(got)
            if want <= 0:
                break
    out = []
    for r in rows:
        m = r.get("metadata") or {}
        text = r.get("source_text") or ""
        out.append({"id": r["vector_id"], "time": m.get("ts") or m.get("created_at"), "text": text[:600],
                    "length": len(text), "meta": {k: m.get(k) for k in _META_KEYS}, "part": part})
    return {"project": project, "part": part, "total": total, "offset": offset, "limit": limit, "sorted": sorted_,
            "items": out}


def delete_items(project: str, part: str, ids: list[str]) -> dict:
    from app import chunks

    deleted = []
    for h, _w in _part_handles(project, part):
        deleted += vs.delete_vt(ids, h)
    sidecars = chunks.delete_for(deleted) if deleted else 0
    return {"project": project, "part": part, "deleted": len(deleted), "ids": deleted, "chunks_deleted": sidecars}


def delete_part(project: str, part: str, confirm: str) -> dict:
    if confirm != project:
        raise HTTPException(status_code=422, detail="confirm must equal the project id")
    if part == "graph":
        if project == config.ORCHESTRATOR_ID:
            raise HTTPException(status_code=403, detail="the orchestrator graph cannot be deleted here")
        from app.graph_db import drop_project_graph

        drop_project_graph(project)
        return {"project": project, "part": "graph", "deleted": True}
    if part not in PARTS:
        raise HTTPException(status_code=422, detail=f"part must be one of {', '.join(PARTS)} or graph")
    n = 0
    for h, w in _part_handles(project, part):
        if w:
            ids = h.get(where=w, include=[]).get("ids") or []
            if ids:
                h.delete(ids=ids)
            n += len(ids)
        else:
            n += int(h.count())
            vs.drop_collection(vs._name_of(h))
    return {"project": project, "part": part, "deleted": n}


# ---------------------------------------------------------------------------
# Lessons
# ---------------------------------------------------------------------------


def lessons(project: str) -> list[dict]:
    from app import distill

    h = vs.get_existing(vs.part_collection_name("knowledge", project))
    if h is None:
        return []
    out = []
    for r in vs.get_vt(h, where={"kind": {"$eq": "lesson"}}):
        m = r["metadata"] or {}
        if m.get("status") == "withdrawn":
            continue
        out.append({"id": r["vector_id"], "title": m.get("title") or "", "text": (r.get("source_text") or "")[:1500],
                    "status": m.get("status"), "category": m.get("category") or m.get("context"),
                    "time": m.get("distilled_at") or m.get("ts"), "by": m.get("distilled_by") or m.get("author")})
    out.sort(key=lambda x: str(x["time"] or ""), reverse=True)
    return out


def add_lesson(project: str, title: str, text: str, model=None) -> dict:
    from app import distill

    title, text = title.strip(), text.strip()
    if not (1 <= len(title) <= distill.title_chars()) or not (1 <= len(text) <= distill.item_chars()):
        raise HTTPException(status_code=422, detail="title must be 1..120 and text 1..600 characters")
    lid = "dk_" + uuid.uuid4().hex[:12]
    now = distill._now()
    meta = {"kind": "lesson", "status": "active", "action": "", "context": "", "entities_json": "[]",
            "entity_key": "-", "dkey": lid, "sources_json": "[]", "evidence_count": 0, "first_seen": now,
            "last_confirmed": now, "distilled_by": f"manual:{_email() or 'local'}", "distilled_at": now}
    body = f"{title}: {text}"
    distill._write(project, lid, meta, title, body, model, distill.encode(body, model))
    return {"id": lid, "project": project, "title": title, "status": "active"}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class PollRequest(BaseModel):
    device_code: str
    code_verifier: str


class RefreshRequest(BaseModel):
    refresh_token: str


class ItemsDelete(BaseModel):
    project: str
    part: str
    ids: list[str] = Field(default_factory=list)


class PartDelete(BaseModel):
    project: str
    part: str
    confirm: str = ""


class MemberRequest(BaseModel):
    project: str
    email: str
    level: Optional[str] = None


class EnsureRequest(BaseModel):
    emails: list[str] = Field(default_factory=list)


class LessonAdd(BaseModel):
    project: str
    title: str
    text: str


class LessonDecide(BaseModel):
    project: str
    ids: list[str] = Field(default_factory=list)
    decision: str
    reason: str = ""


def attach(app: FastAPI) -> None:
    @app.get("/home", response_class=HTMLResponse, tags=["home"])
    def home_page():
        try:
            with open(STATIC, encoding="utf-8") as f:
                html = f.read()
        except OSError:
            raise HTTPException(status_code=404, detail="home page missing")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/home/config", tags=["home"])
    def home_config():
        from app.embeddings import get_embedder

        iss = _issuer()
        return {"auth": bool(iss), "client_id": _client_id(), "base": iss, "kh_version": config.KH_VERSION,
                "emb_model": getattr(get_embedder(), "name", config.EMBEDDING_MODEL)}

    @app.post("/home/login/start", tags=["home"])
    def home_login_start():
        return login_start()

    @app.post("/home/login/poll", tags=["home"])
    def home_login_poll(body: PollRequest):
        return _token({"grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": body.device_code,
                       "code_verifier": body.code_verifier})

    @app.post("/home/login/refresh", tags=["home"])
    def home_login_refresh(body: RefreshRequest):
        return _token({"grant_type": "refresh_token", "refresh_token": body.refresh_token})

    @app.get("/home/projects", tags=["home"])
    def home_projects():
        return {"projects": projects_overview()}

    @app.get("/home/items", tags=["home"])
    def home_items(project: str, part: str = "knowledge", offset: int = 0, limit: int = 50):
        return items(project, part, max(0, offset), limit)

    @app.post("/home/items/delete", tags=["home"])
    def home_items_delete(body: ItemsDelete):
        return delete_items(body.project, body.part, body.ids)

    @app.post("/home/parts/delete", tags=["home"])
    def home_parts_delete(body: PartDelete):
        return delete_part(body.project, body.part, body.confirm)

    @app.get("/home/members", tags=["home"])
    def home_members(project: str):
        from app import sync

        return sync._identity("GET", f"/projects/{project}/members")

    @app.put("/home/members", tags=["home"])
    def home_members_put(body: MemberRequest):
        from app import sync

        if body.level not in ("owner", "editable", "readonly"):
            raise HTTPException(status_code=422, detail="level must be owner, editable or readonly")
        return sync._identity("PUT", f"/projects/{body.project}/members",
                              {"email": body.email, "level": body.level})

    @app.post("/home/members/remove", tags=["home"])
    def home_members_remove(body: MemberRequest):
        from app import sync

        return sync._identity("DELETE", f"/projects/{body.project}/members/{body.email}")

    @app.post("/home/personal/ensure", tags=["home"])
    def home_personal_ensure(body: EnsureRequest):
        from app import personal

        out = []
        for e in body.emails:
            e = (e or "").strip().lower()
            if "@" not in e:
                continue
            personal.touch(e)
            out.append({"email": e, "project": personal.personal_id(e)})
        return {"ensured": out}

    @app.get("/home/lessons", tags=["home"])
    def home_lessons(project: str):
        ls = lessons(project)
        return {"project": project, "count": len(ls), "lessons": ls}

    @app.post("/home/lessons", tags=["home"])
    def home_lessons_add(body: LessonAdd):
        return add_lesson(body.project, body.title, body.text, getattr(app.state, "model", None))

    @app.post("/home/lessons/decide", tags=["home"])
    def home_lessons_decide(body: LessonDecide):
        from app import distill

        if body.decision == "approve":
            return distill.approve(body.project, body.ids, [], model=getattr(app.state, "model", None))
        if body.decision == "reject":
            return distill.reject(body.project, body.ids, body.reason)
        raise HTTPException(status_code=422, detail="decision must be approve or reject")


