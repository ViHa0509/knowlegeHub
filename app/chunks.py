"""Stored chunk vectors for long rows (spec §1.8b): sidecar `kh_chunk_<owner project>`, ids `<row_id>#c<n>`.

Public names: windows(text), windows_at(text), collection_name(project), write_for(row_id, text, meta, owner),
delete_for(ids), lanes(scope, project), CHUNK_SCOPE.
"""
from __future__ import annotations

import logging
from typing import Sequence

from app import config

log = logging.getLogger(__name__)

CHUNK_SCOPE = "chunk"


def windows_at(text: str) -> list[tuple[int, str]]:
    """[(start, piece)] beyond the head window; [] for rows <= KH_CHUNK_MIN_CHARS."""
    text = text or ""
    n = len(text)
    if n <= config.CHUNK_MIN_CHARS:
        return []
    size, stride = config.CHUNK_CHARS, config.CHUNK_STRIDE
    out: list[tuple[int, str]] = []
    start = stride
    while start < n:
        piece = text[start:start + size]
        if len(piece.strip()) >= 200 or not out:
            out.append((start, piece))
        if start + size >= n:
            break
        start += stride
    return out


def windows(text: str) -> list[str]:
    return [p for _, p in windows_at(text)]


def collection_name(project: str | None) -> str:
    from app import vector_store as vs

    return vs.part_collection_name(CHUNK_SCOPE, project)


def write_for(row_id: str, text: str, meta: dict, owner: str | None, model=None) -> int:
    """(Re)write the chunk vectors of one row. Old chunks of the row are deleted first. Returns chunks written."""
    from app import vector_store as vs
    from app.embeddings import get_embedder

    name = collection_name(owner)
    wins = windows(text)
    existing = vs.get_existing(name)
    if existing is not None:
        old = existing.get(where={"chunk_of": {"$eq": row_id}}, include=[]).get("ids") or []
        if old:
            existing.delete(ids=old)
    if not wins:
        return 0
    model = model or get_embedder()
    vecs = model.encode(wins, normalize_embeddings=True)
    coll = vs.get_or_create(name)
    base = {k: v for k, v in (meta or {}).items() if k != "source_text"}
    ids, metas = [], []
    for i, _piece in enumerate(wins, start=1):
        ids.append(f"{row_id}#c{i}")
        m = dict(base)
        m["chunk_of"] = row_id
        m["chunk_no"] = i
        metas.append(m)
    vs.upsert_rows(coll, ids, vecs, wins, metas)
    return len(ids)


def _chunk_collections() -> list[str]:
    from app import vector_store as vs

    prefix = vs.scope_prefix(CHUNK_SCOPE)
    return [n for n in vs.list_collection_names() if n.startswith(prefix)]


def delete_for(ids: Sequence[str]) -> int:
    """Remove the chunks of these rows from every kh_chunk_* collection."""
    from app import vector_store as vs

    ids = [i for i in ids if i]
    if not ids:
        return 0
    n = 0
    for name in _chunk_collections():
        coll = vs.get_existing(name)
        if coll is None:
            continue
        found = coll.get(where={"chunk_of": {"$in": list(ids)}}, include=[]).get("ids") or []
        if found:
            coll.delete(ids=found)
            n += len(found)
    return n


def lanes(scope: str | None, project: str | None) -> list:
    """Chunk sidecar collections a read covers (only when a scope is given)."""
    from app import auth
    from app import vector_store as vs

    if not scope:
        return []
    project = vs.normalize_project(project)
    if project:
        h = vs.get_existing(collection_name(project))
        return [h] if h is not None else []
    if scope in vs.COMMON_SCOPES:
        h = vs.get_existing(collection_name(vs.ORCHESTRATOR))
        return [h] if h is not None else []
    visible = auth.visible_projects("project.read")
    out = []
    prefix = vs.scope_prefix(CHUNK_SCOPE)
    for name in _chunk_collections():
        pid = name[len(prefix):]
        if visible is None or pid in visible or pid == vs.ORCHESTRATOR:
            h = vs.get_existing(name)
            if h is not None:
                out.append(h)
    return out
