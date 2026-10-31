"""Entity cards (spec §3.6): vector rows in kh_entity_<project>. Route group `entities`.

    upsert(project, name, ...) / match(text, projects, limit) / line_of(card) / get_card(project, name, depth)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app import ctx_util as U
from app.ctx_util import fold

log = logging.getLogger(__name__)

TYPES = ("person", "system", "module", "tool", "org", "rule", "other")
MAX_MATCH = 20
MAX_DEPTH = 2
MAX_SOURCES = 50
MAX_RELATIONS_SHOWN = 6
SCAN_LIMIT = 100000


def entity_id(project: str, name: str) -> str:
    return f"ent__{project}__{fold(name).replace(' ', '-')}"


def _coll(project: str, create: bool = False):
    from app import vector_store as vs

    name = vs.part_collection_name("entity", project)
    return vs.get_or_create(name) if create else vs.get_existing(name)


def card_of(rid: str, meta: dict) -> dict:
    return {
        "id": rid, "project": meta.get("project"), "name": meta.get("name"),
        "type": meta.get("entity_type") or "other", "role": meta.get("role") or "",
        "purpose": meta.get("purpose") or "", "aliases": U.jload(meta.get("aliases_json"), []),
        "relations": U.jload(meta.get("relations_json"), []), "sources": U.jload(meta.get("sources_json"), []),
        "first_seen": meta.get("first_seen"), "last_seen": meta.get("last_seen"),
    }


def line_of(card: dict) -> str:
    s = f"{card.get('name')} ({card.get('type') or 'entity'}, {card.get('project')})"
    if card.get("role"):
        s += f" - role: {card['role']}"
    if card.get("purpose"):
        s += f" - purpose: {card['purpose']}"
    rels = [f"{r.get('rel')} {r.get('target')}" for r in (card.get("relations") or [])[:MAX_RELATIONS_SHOWN]]
    if rels:
        s += " - relations: " + "; ".join(rels)
    return s


def text_of(card: dict) -> str:
    t = f"ENTITY {line_of(card)}."
    if card.get("aliases"):
        t += " Also known as: " + ", ".join(card["aliases"]) + "."
    return t


def upsert(project: str, name: str, type_: Optional[str] = None, role: Optional[str] = None,
           purpose: Optional[str] = None, aliases=None, relations=None, source_log_ids=None, model=None) -> dict:
    from app import vector_store as vs

    if model is None:
        from app.embeddings import get_embedder

        model = get_embedder()
    rid = entity_id(project, name)
    coll = _coll(project, create=True)
    old_rows = vs.get_ids_vt(coll, [rid])
    old = card_of(rid, old_rows[0]["metadata"]) if old_rows else None
    now = U.now_iso()
    card = old or {"id": rid, "project": project, "name": name, "type": "other", "role": "", "purpose": "",
                   "aliases": [], "relations": [], "sources": [], "first_seen": now}
    card["name"] = name
    card["type"] = (type_ or "other") if (type_ or "other") in TYPES else "other"
    if role:
        card["role"] = role
    if purpose:
        card["purpose"] = purpose
    al = list(card.get("aliases") or [])
    have = {fold(a) for a in al} | {fold(name)}
    for a in aliases or []:
        a = str(a).strip()
        if a and fold(a) not in have:
            al.append(a)
            have.add(fold(a))
    card["aliases"] = al
    rels = list(card.get("relations") or [])
    rkeys = {(str(r.get("rel")).lower(), fold(r.get("target"))) for r in rels}
    for r in relations or []:
        rel, tgt = str(r.get("rel") or "").strip().lower(), str(r.get("target") or "").strip()
        if not rel or not tgt or (rel, fold(tgt)) in rkeys:
            continue
        rels.append({"rel": rel, "target": tgt})
        rkeys.add((rel, fold(tgt)))
    card["relations"] = rels
    src = list(card.get("sources") or [])
    for s in source_log_ids or []:
        if s and s not in src:
            src.append(str(s))
    card["sources"] = src[-MAX_SOURCES:]
    card["first_seen"] = card.get("first_seen") or now
    card["last_seen"] = now
    meta = {"scope": "entity", "knowledge_type": "quick", "kind": "entity", "project": project, "name": name,
            "entity_type": card["type"], "role": card["role"], "purpose": card["purpose"],
            "aliases_json": json.dumps(card["aliases"]), "relations_json": json.dumps(card["relations"]),
            "sources_json": json.dumps(card["sources"]), "first_seen": card["first_seen"],
            "last_seen": card["last_seen"]}
    actor = None
    try:
        from app import auth

        actor = auth.current_actor()
        p = auth.current()
        if p is not None:
            meta["author"] = p.email
    except Exception:  # noqa: BLE001
        pass
    if actor:
        meta["actor"] = actor
    text = text_of(card)
    vs.upsert_one(coll, rid, model.encode(text, normalize_embeddings=True), text, meta)
    return {"id": rid, "updated": old is not None, "card": card, "line": line_of(card)}


def cards_of(project: str) -> list[dict]:
    from app import vector_store as vs

    h = _coll(project)
    if h is None:
        return []
    rows = vs.meta_rows_vt(h, {"kind": {"$eq": "entity"}})
    return [card_of(rid, m) for rid, m in rows[:SCAN_LIMIT]]


def match(text: str, projects: list[str], limit: int = 8) -> list[dict]:
    hay = " " + fold(text) + " "
    hits = []
    for p in projects or []:
        if not p:
            continue
        for card in cards_of(p):
            best = ""
            for term in [card.get("name") or "", *(card.get("aliases") or [])]:
                f = fold(term)
                if f and (" " + f + " ") in hay and len(f) > len(best):
                    best = f
            if best:
                hits.append((len(best), card))
    hits.sort(key=lambda x: (-x[0], str(x[1].get("name") or "")))
    return [c for _n, c in hits[:max(0, min(int(limit), MAX_MATCH))]]


def find_card(project: str, name: str) -> Optional[dict]:
    f = fold(name)
    for c in cards_of(project):
        if fold(c.get("name")) == f or any(fold(a) == f for a in c.get("aliases") or []):
            return c
    return None


def get_card(project: str, name: str, depth: int = 1) -> Optional[dict]:
    root = find_card(project, name)
    if root is None:
        return None
    depth = max(0, min(int(depth), MAX_DEPTH))
    seen = {root["id"]}
    linked: list[dict] = []
    frontier = [root]
    for _ in range(depth):
        nxt = []
        for c in frontier:
            for r in c.get("relations") or []:
                t = find_card(project, r.get("target") or "")
                if t is not None and t["id"] not in seen:
                    seen.add(t["id"])
                    linked.append(t)
                    nxt.append(t)
        frontier = nxt
    return {"entity": root, "linked": linked, "lines": [line_of(root)] + [line_of(c) for c in linked]}


class Relation(BaseModel):
    rel: str = Field(..., min_length=1, max_length=80)
    target: str = Field(..., min_length=1, max_length=200)


class EntityBody(BaseModel):
    project: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1, max_length=200)
    type: Optional[str] = "other"
    role: Optional[str] = None
    purpose: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    source_log_ids: list[str] = Field(default_factory=list)


def attach(app: FastAPI) -> None:
    @app.post("/entities", tags=["entities"])
    async def entities_upsert(body: EntityBody, request: Request):
        U.need(body.project, "project.write")
        if body.type and body.type not in TYPES:
            raise HTTPException(status_code=422, detail="type must be one of " + ", ".join(TYPES))
        model = U.model_of(request)
        return await asyncio.to_thread(upsert, body.project, body.name, body.type, body.role, body.purpose,
                                       body.aliases, [r.model_dump() for r in body.relations], body.source_log_ids,
                                       model)

    @app.get("/entities/match", tags=["entities"])
    def entities_match(text: str, project: Optional[str] = None, limit: int = 8):
        if project:
            U.need(project, "project.read")
            scope = [project]
        else:
            scope = U.readable_projects()
        cards = match(text, scope, limit)
        return {"count": len(cards), "lines": [line_of(c) for c in cards], "cards": cards}

    @app.get("/entities/{project}/{name}", tags=["entities"])
    def entities_get(project: str, name: str, depth: int = 1):
        U.need(project, "project.read")
        if depth < 0 or depth > MAX_DEPTH:
            raise HTTPException(status_code=422, detail=f"depth must be 0..{MAX_DEPTH}")
        out = get_card(project, name, depth)
        if out is None:
            raise HTTPException(status_code=404, detail=f"no entity '{name}' in project '{project}'")
        return out
