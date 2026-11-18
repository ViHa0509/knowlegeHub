"""KH 7.1 local sync pages: POST /sync/dump (one page of a project's part, with embeddings) and
POST /sync/import (merge such a page into this KH). Maintainers only; used to move a project between
KH installs without a sync key (copy the pages as files).
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import ctx_util as U
from app import projects, sync
from app import vector_store as vs

PARTS = ["knowledge", "log", "hotlog", "findings", "entity", "chunk", "personalization", "policy", "src"]


def _check_part(part: str) -> str:
    if part not in PARTS:
        raise HTTPException(status_code=422, detail=f"part must be one of {PARTS}")
    return part


def _local_model() -> tuple[str, int]:
    h = sync.hello()
    return h["model"], int(h["dim"])


class DumpBody(BaseModel):
    part: str = ""
    project: Optional[str] = None
    offset: int = 0
    limit: int = sync.PAGE_DEFAULT
    since: Optional[str] = None
    until: Optional[str] = None
    include_undated: bool = True


class ImportBody(BaseModel):
    project: Optional[str] = None
    part: str = ""
    rows: list[dict[str, Any]] = []
    emb_model: Optional[str] = None
    emb_dim: Optional[int] = None
    dry_run: bool = False


def dump(b: DumpBody) -> dict:
    part = _check_part(b.part)
    pid = vs.normalize_project(b.project)
    if not pid:
        raise HTTPException(status_code=422, detail="project is required")
    real = sync.config.HOTLOG_SCOPE if part == "hotlog" else part
    page = sync.export_rows(pid, real, max(0, b.offset), b.limit, b.since, b.until, b.include_undated)
    model, dim = _local_model()
    rows = [{"id": r["id"], "document": r.get("text"), "metadata": r.get("metadata") or {},
             "embedding": r.get("embedding")} for r in page["rows"]]
    return {"project": pid, "part": part, "rows": rows, "scanned": len(rows), "next_offset": page["next_offset"],
            "total": page["total"], "emb_model": model, "emb_dim": dim}


def do_import(b: ImportBody) -> dict:
    model, dim = _local_model()
    if (b.emb_model or "") != model or int(b.emb_dim or 0) != dim:
        made = f"{b.emb_model or ''} {b.emb_dim if b.emb_dim is not None else '[]'}".strip()
        raise HTTPException(status_code=409, detail=f"page made with {made}, this KH runs {model} {dim}")
    part = _check_part(b.part)
    pid = vs.normalize_project(b.project)
    if not pid:
        raise HTTPException(status_code=422, detail="project is required")
    result = "exists"
    if not projects.exists(pid):
        result = "would create" if b.dry_run else "created"
        if not b.dry_run:
            projects.get_or_create_project(pid, source="sync")
    no_emb = sum(1 for r in b.rows if not r.get("embedding"))
    rows = []
    for r in b.rows:
        meta = dict(r.get("metadata") or {})
        if "project" in meta:
            meta["project"] = pid
        rows.append(dict(r, metadata=meta))
    c = sync.merge_rows(pid, part, rows, dry=b.dry_run)
    counts = {"created": c["created"], "updated": c["updated"], "unchanged": c["unchanged"],
              "skipped_newer": c["kept"], "no_embedding": no_emb}
    return {"project": pid, "part": part, "project_result": result, "counts": counts, "dry_run": b.dry_run}


def attach(app: FastAPI) -> None:
    @app.post("/sync/dump", tags=["sync"])
    def sync_dump(body: DumpBody):
        U.need_maintainer()
        return dump(body)

    @app.post("/sync/import", tags=["sync"])
    def sync_import(body: ImportBody):
        U.need_maintainer()
        return do_import(body)
