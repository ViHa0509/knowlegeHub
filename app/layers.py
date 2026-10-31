"""Layers module (spec §1.14 "C4 layers", §3.11): one piece of knowledge stored as zoom levels.

Rows live in `kh_know_<project>` (orchestrator when none), tagged scope "knowledge", metadata
{layered:"1", tree_id, lv, parent_id, epoch, kind, subject_key, author, verified_count, ts}. They carry no
`knowledge_type="quick"`, so /vector/query never sees them.

Public names: slug, should_layer, dynamic_k, reconcile, author_rank, add, query, gather, LAYER_MIN_WORDS.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone

log = logging.getLogger(__name__)

LAYER_MIN_WORDS = 25
DROP_MAX = 0.15
SCORE_MIN = 0.30
K_MAX = 20
_AUTHOR_RANK = {"user": 0, "agent": 1, "trained": 2}


def slug(text: str, prefix: str = "kb") -> str:
    low = (text or "").lower()
    body = re.sub("[^a-z0-9]+", "_", low)[:40]
    return f"{prefix}_{body}_{hashlib.sha1((text or '').encode('utf-8')).hexdigest()[:8]}"


def should_layer(text: str) -> bool:
    return len((text or "").split()) >= LAYER_MIN_WORDS


def dynamic_k(hits: list[dict]) -> list[dict]:
    """Leading run of hits: stop at a score drop > 0.15 or a score < 0.30; max 20; never empty."""
    if not hits:
        return []
    out = [hits[0]]
    for prev, h in zip(hits, hits[1:]):
        if len(out) >= K_MAX:
            break
        if h["score"] < SCORE_MIN or prev["score"] - h["score"] > DROP_MAX:
            break
        out.append(h)
    return out


def author_rank(author: str | None) -> int:
    kind = (author or "").split(":", 1)[0].strip().lower()
    return _AUTHOR_RANK.get(kind, len(_AUTHOR_RANK))


def _ts(v) -> float:
    from app.hotlog import parse_ts

    return parse_ts(v)


def reconcile(hits: list[dict]) -> list[dict]:
    """Per subject_key|lv keep one winner: author (user < agent < trained), then verified_count, then latest ts."""
    best: dict[str, dict] = {}
    order: list[str] = []
    for h in hits:
        m = h.get("metadata") or {}
        key = f"{m.get('subject_key', h.get('subject_key'))}|{m.get('lv', h.get('lv'))}"
        rank = (author_rank(m.get("author")), -int(m.get("verified_count") or 0), -_ts(m.get("ts")))
        cur = best.get(key)
        if cur is None:
            order.append(key)
            best[key] = {**h, "_rank": rank}
        elif rank < cur["_rank"]:
            best[key] = {**h, "_rank": rank}
    out = []
    for k in order:
        h = best[k]
        h.pop("_rank", None)
        out.append(h)
    return out


def _store(project: str | None):
    from app import vector_store as vs

    return vs.get_or_create(vs.part_collection_name("knowledge", vs.own_project(project)))


def _store_existing(project: str | None):
    from app import vector_store as vs

    return vs.get_existing(vs.part_collection_name("knowledge", vs.own_project(project)))


def _hit(rid: str, score: float, text: str | None, meta: dict) -> dict:
    return {"vector_id": rid, "score": score, "text": text or meta.get("source_text") or "",
            "lv": meta.get("lv"), "tree_id": meta.get("tree_id"), "parent_id": meta.get("parent_id"),
            "subject_key": meta.get("subject_key"), "metadata": meta}


def _public(h: dict) -> dict:
    return {k: h[k] for k in ("vector_id", "score", "text", "lv", "tree_id", "parent_id", "subject_key")}


def add(text: str, kind: str = "knowledge", subject_key: str = "", author: str = "agent:kh", epoch: int = 1,
        ts: str = "", project: str | None = None, model=None) -> dict:
    """Store one lv1 record `<tree_id>_lv1`; attach under an existing tree of the same subject."""
    from app import vector_store as vs
    from app.embeddings import get_embedder

    model = model or get_embedder()
    tree_id = slug(text)
    subject = subject_key or slug(text, "subj")
    coll = _store(project)
    attached_to = None
    try:
        same = coll.get(where={"$and": [{"layered": {"$eq": "1"}}, {"subject_key": {"$eq": subject}}]},
                        include=["metadatas"])
        for m in same.get("metadatas") or []:
            t = (m or {}).get("tree_id")
            if t and t != tree_id:
                attached_to = t
                break
    except Exception as e:  # noqa: BLE001
        log.warning("layers: subject lookup failed: %s", e)
    rid = f"{tree_id}_lv1"
    meta = {
        "scope": "knowledge", "project": vs.own_project(project), "layered": "1", "tree_id": tree_id, "lv": 1,
        "parent_id": "", "epoch": int(epoch), "kind": kind, "subject_key": subject, "author": author,
        "verified_count": 0, "ts": ts or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if attached_to:
        meta["attached_to"] = attached_to
    vec = model.encode(text, normalize_embeddings=True)
    vs.upsert_one(coll, rid, vec, text, meta)
    return {"tree_id": tree_id, "subject_key": subject, "kind": kind, "layered": False,
            "attached_to": attached_to, "records": [{"id": rid, "lv": 1, "text": text, "parent_id": ""}]}


def query(text: str, max_level: int = 1, k: int = 5, tree_id: str = "", kind: str = "",
          project: str | None = None, model=None) -> dict:
    from app import vector_store as vs
    from app.embeddings import get_embedder

    try:
        coll = _store_existing(project)
        if coll is None:
            return {"hits": []}
        model = model or get_embedder()
        where = vs.and_where({"layered": {"$eq": "1"}}, {"lv": {"$lte": int(max_level)}},
                             {"tree_id": {"$eq": tree_id}} if tree_id else None,
                             {"kind": {"$eq": kind}} if kind else None)
        hits = vs.query_vt(coll, model.encode(text, normalize_embeddings=True), max(1, int(k)), where=where)
        hits = [_hit(h["vector_id"], h["score"], h["source_text"], h["metadata"]) for h in hits]
        return {"hits": [_public(h) for h in dynamic_k(hits)]}
    except Exception as e:  # noqa: BLE001
        log.warning("layers.query failed: %s", e)
        return {"hits": []}


def gather(subject_key: str, do_reconcile: bool = True, max_level: int | None = None,
           project: str | None = None) -> dict:
    from app import vector_store as vs

    try:
        coll = _store_existing(project)
        if coll is None:
            return {"hits": []}
        where = vs.and_where({"layered": {"$eq": "1"}}, {"subject_key": {"$eq": subject_key}},
                             {"lv": {"$lte": int(max_level)}} if max_level is not None else None)
        rows = vs.get_vt(coll, where=where)
        hits = [_hit(r["vector_id"], 1.0, r["source_text"], r["metadata"]) for r in rows]
        if do_reconcile:
            hits = reconcile(hits)
        hits.sort(key=lambda h: (h["lv"] or 0, h["vector_id"]))
        return {"hits": [_public(h) for h in hits]}
    except Exception as e:  # noqa: BLE001
        log.warning("layers.gather failed: %s", e)
        return {"hits": []}
