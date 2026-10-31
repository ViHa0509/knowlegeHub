"""Revisions (spec §4.12): supersede-with-history in the project's knowledge collection.

`POST /knowledge/revise {subject, text, project?, scope?, ts?, author?}` stores a new current revision of a subject
and relabels the prior current rows (`rev_current "0"`, `superseded_by`); they stay `knowledge_type: quick`, so old
values remain findable, labelled. `POST /knowledge/history {subject, project?}` lists the chain, current first.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app import auth
from app import vector_store as vs
from app.embeddings import EmbedderUnavailable, get_embedder

log = logging.getLogger(__name__)

DEFAULT_SCOPE = "knowledge"


class ReviseRequest(BaseModel):
    subject: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    project: Optional[str] = None
    scope: Optional[str] = None
    ts: Optional[str] = None
    author: Optional[str] = None


class HistoryRequest(BaseModel):
    subject: str = Field(..., min_length=1)
    project: Optional[str] = None
    scope: Optional[str] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def revision_id(subject: str, text: str, ts: str) -> str:
    h = hashlib.sha1(f"{subject}\x1f{text}\x1f{ts}".encode("utf-8")).hexdigest()[:12]
    return f"rev_{subject}_{h}"


def _encode(model, text: str):
    enc = model if model is not None and hasattr(model, "encode") else get_embedder()
    return enc.encode(text, normalize_embeddings=True)


def revise(body: ReviseRequest, model=None) -> dict:
    subject = body.subject.strip()
    if not subject:
        raise HTTPException(status_code=422, detail="subject must not be empty")
    scope = (body.scope or DEFAULT_SCOPE).strip() or DEFAULT_SCOPE
    project = vs.own_project(body.project)
    ts = (body.ts or "").strip() or _now()
    principal = auth.current()
    author = principal.email if principal is not None else (body.author or None)
    new_id = revision_id(subject, body.text, ts)
    try:
        vec = _encode(model, body.text)
    except EmbedderUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Embedding failed: {e}")
    coll = vs.scope_collection(scope, project)
    prior = [rid for rid, _m in vs.meta_rows_vt(coll, {"$and": [{"revsubject": subject}, {"rev_current": "1"}]})
             if rid != new_id]
    meta = {"knowledge_type": "quick", "scope": scope, "project": project, "revsubject": subject,
            "rev_current": "1", "rev_ts": ts, "author": author or "", "superseded_by": ""}
    actor = auth.current_actor()
    if actor:
        meta["actor"] = actor
    try:
        vs.upsert_one(coll, new_id, vec, body.text, meta)
        if prior:
            rows = {r["vector_id"]: r for r in vs.get_ids_vt(coll, prior)}
            ids = [i for i in prior if i in rows]
            if ids:
                metas = []
                for i in ids:
                    m = dict(rows[i].get("metadata") or {})
                    m.update({"rev_current": "0", "superseded_by": new_id, "knowledge_type": "quick"})
                    metas.append(m)
                coll.update(ids=ids, metadatas=metas)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("revision write failed")
        raise HTTPException(status_code=500, detail=f"Vector store error: {e}")
    chain = vs.meta_rows_vt(coll, {"revsubject": subject})
    return {"subject": subject, "revision_id": new_id, "superseded": prior, "chain_len": len(chain)}


def history(body: HistoryRequest) -> dict:
    subject = body.subject.strip()
    scope = (body.scope or DEFAULT_SCOPE).strip() or DEFAULT_SCOPE
    name = vs.scope_collection_name(scope, vs.own_project(body.project))
    coll = vs.get_existing(name) if name else None
    rows = vs.get_vt(coll, {"revsubject": subject}) if coll is not None else []
    revs = []
    for r in rows:
        m = r.get("metadata") or {}
        revs.append({"revision_id": r["vector_id"], "text": r.get("source_text") or "",
                     "ts": m.get("rev_ts") or "", "current": str(m.get("rev_current")) == "1",
                     "superseded_by": m.get("superseded_by") or "", "author": m.get("author") or ""})
    revs.sort(key=lambda x: x["ts"], reverse=True)
    revs.sort(key=lambda x: not x["current"])
    current = next((x["revision_id"] for x in revs if x["current"]), None)
    return {"subject": subject, "count": len(revs), "current": current, "revisions": revs}


def attach(app: FastAPI) -> None:
    @app.post("/knowledge/revise", tags=["knowledge"])
    def knowledge_revise(body: ReviseRequest, request: Request):
        return revise(body, getattr(request.app.state, "model", None))

    @app.post("/knowledge/history", tags=["knowledge"])
    def knowledge_history(body: HistoryRequest):
        return history(body)
