"""Semantic query pipeline (spec §1.10, §3.2). Pure vector search, no LLM.

Public names:
    build_quick_where(scope, where)                 {"knowledge_type":"quick"} AND scope AND caller where
    quick_collections(scope, project)               twin of vector_store.read_collections
    query_quick_knowledge(text, k, model=None, scope=None, where=None, project=None)
        -> [{vector_id, score, source_text, summary, piece, metadata}]
    clip_hit(hit, max_chars, query) -> hit with source_text clipped, truncated, text_len, piece_at
    fetch_graph(core_ids) -> (nodes, edges) on the default graph
"""
from __future__ import annotations

import logging
import re

from app import config

log = logging.getLogger(__name__)

SEP = " [...] "


def build_quick_where(scope: str | None, where: dict | None) -> dict:
    from app import vector_store as vs

    return vs.and_where({"knowledge_type": {"$eq": "quick"}},
                        {"scope": {"$eq": scope}} if scope else None, where)


def quick_collections(scope: str | None, project: str | None) -> list:
    from app import vector_store as vs

    return vs.read_collections(scope, project)


def _lanes(scope, project, where):
    from app import chunks, hotlog

    lanes = [(h, where, False) for h in quick_collections(scope, project)]
    hot = hotlog.read_lane(scope, project, where)
    if hot is not None:
        lanes.append((hot[0], hot[1], False))
    for h in chunks.lanes(scope, project):
        lanes.append((h, where, True))
    return lanes


def query_quick_knowledge(text: str, k: int = 1, model=None, scope: str | None = None, where: dict | None = None,
                          project: str | None = None) -> list[dict]:
    from app import auth
    from app import vector_store as vs
    from app.chunker import semantic_chunks
    from app.embeddings import get_embedder

    model = model or get_embedder()
    k = max(1, int(k))
    wf = build_quick_where(scope, where)
    lanes = _lanes(scope, project, wf)

    # Plain encode (NOT encode_query): queries and passages are encoded the same way (§1.18).
    if len((text or "").split()) > config.CHUNK_WORD_THRESHOLD:
        pieces = semantic_chunks(text, 1.0, model) or [text]
    else:
        pieces = [text]
    vecs = model.encode(pieces, normalize_embeddings=True)

    best: dict[str, dict] = {}
    for vec in vecs:
        for coll, lw, is_chunk in lanes:
            try:
                hits = vs.query_vt(coll, vec, 3 * k if is_chunk else k, where=lw)
            except Exception as e:  # noqa: BLE001
                log.warning("lane %s failed: %s", getattr(coll, "name", coll), e)
                continue
            for h in hits:
                if is_chunk:
                    rid = h["metadata"].get("chunk_of") or h["vector_id"].split("#c")[0]
                    cand = {"vector_id": rid, "score": h["score"], "source_text": None,
                            "piece": h["source_text"], "metadata": None}
                else:
                    rid = h["vector_id"]
                    cand = {"vector_id": rid, "score": h["score"], "source_text": h["source_text"],
                            "piece": None, "metadata": h["metadata"]}
                cur = best.get(rid)
                if cur is None:
                    best[rid] = cand
                    continue
                if cand["score"] > cur["score"]:
                    cur["score"] = cand["score"]
                    if cand["piece"] is not None:
                        cur["piece"] = cand["piece"]
                if cur["metadata"] is None and cand["metadata"] is not None:
                    cur["metadata"] = cand["metadata"]
                    cur["source_text"] = cand["source_text"]

    # Rows found only via chunks: re-read through the same lanes (never resurrect deleted/invisible rows).
    orphans = [rid for rid, c in best.items() if c["metadata"] is None]
    if orphans:
        need = set(orphans)
        for coll, lw, is_chunk in lanes:
            if is_chunk or not need:
                continue
            try:
                for r in vs.get_ids_vt(coll, sorted(need), lw):
                    c = best[r["vector_id"]]
                    c["metadata"] = r["metadata"]
                    c["source_text"] = r["source_text"]
                    need.discard(r["vector_id"])
            except Exception as e:  # noqa: BLE001
                log.warning("re-read on %s failed: %s", getattr(coll, "name", coll), e)
        for rid in need:
            best.pop(rid, None)

    out = []
    for c in best.values():
        meta = c["metadata"] or {}
        if not auth.acting_visible(meta):
            continue
        c["summary"] = meta.get("summary") or ""
        out.append(c)
    out.sort(key=lambda h: h["score"], reverse=True)
    return out[:k]


_QWORD = re.compile(r"[A-Za-z0-9_]+")


def _best_offset(piece: str, room: int, query: str | None) -> int:
    words = {w.lower() for w in _QWORD.findall(query or "") if len(w) > 3}
    if not words or len(piece) <= room:
        return 0
    best_at, best_n = 0, -1
    for at in range(0, max(1, len(piece) - room + 1), 50):
        window = piece[at:at + room].lower()
        n = sum(1 for w in words if w in window)
        if n > best_n:
            best_at, best_n = at, n
    return best_at


def clip_hit(hit: dict, max_chars: int | None, query: str | None = None) -> dict:
    """Snippet clipping (§1.10). Mutates and returns `hit`."""
    text = hit.get("source_text") or ""
    hit.setdefault("truncated", False)
    hit.setdefault("text_len", len(text))
    hit.setdefault("piece_at", None)
    if not max_chars or len(text) <= max_chars:
        return hit
    piece = hit.get("piece")
    room = max_chars - config.CLIP_HEAD_CHARS - len(SEP)
    start = text.find(piece) if piece else -1
    if piece and start >= 0 and start + len(piece) > max_chars and room >= 100:
        at = start + _best_offset(piece, room, query)
        hit["source_text"] = text[:config.CLIP_HEAD_CHARS] + SEP + text[at:at + room]
        hit["piece_at"] = at
    else:
        hit["source_text"] = text[:max_chars]
    hit["truncated"] = True
    hit["text_len"] = len(text)
    return hit


def fetch_graph(core_ids, hops: int | None = None):
    from app.graph_db import graph_db

    try:
        return graph_db.fetch_graph(list(core_ids), config.HIERARCHY_EDGES,
                                    config.ONTOLOGY_HOPS if hops is None else hops)
    except Exception as e:  # noqa: BLE001
        log.warning("fetch_graph failed: %s", e)
        return [], []
