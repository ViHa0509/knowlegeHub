"""Vector store (spec §1.7): Chroma-style client, per-project collections, routing and enumeration primitives.

Everything returns plain dicts. Main names other areas should use:

  Collections / naming
    shared_collection()                    legacy shared collection `knowledge_hub_vectors` (the "None lane")
    get_or_create(name) / get_existing(name) / collection_exists(name) / drop_collection(name)
    list_collection_names()
    part_collection_name(scope, project)   kh_log_/kh_know_/kh_find_/kh_policy_/kh_<scope>_<id>
    normalize_project(p) / own_project(p)  alias -> orchestrator; blank -> orchestrator
    scope_collection_name(scope, project)  write routing (None = shared)
    scope_collection(scope, project)       write routing, returns a handle (created on demand)
    scope_collections_all(scope, visible)  existing per-project parts of a scope (+ orchestrator)
    every_collection()                     shared + every existing collection

  Read routing (open mode: everything visible; uses app.auth when sign-in is on)
    read_collections(scope, project)       [handles]  (twin of retriever._quick_collections)
    read_lanes(scope, project, where)      [(handle, where)] + hot-window lane; 403 if project unreadable
    and_where(*wheres)

  Primitives on a handle
    upsert_rows(coll, ids, vectors, texts, metas)   stamps emb_* + source_text
    query_vt, get_vt, get_ids_vt, meta_rows_vt, field_values_vt, scroll_vt, topics_vt,
    backfill_topic_vt, fill_missing_field_vt, delete_vt, move_rows

  Keys:  keys_collection(project), keys_add, keys_query, keys_delete

  Legacy source lanes (LEGACY): src_collection(source), upsert_src, upsert_src_batch, query_src, keyword_src,
    keyword_terms, hybrid_src, hub_upsert / hub_get / hub_query / hub_delete (`kh_orch_hub`)
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Iterable, Sequence

from app import config

log = logging.getLogger(__name__)

ORCHESTRATOR = config.ORCHESTRATOR_ID
COSINE = {"hnsw:space": "cosine"}
COMMON_SCOPES = ("personalization", "kh-meta", "self-info")
ORCH_ALIASES = {"kh-meta", "self-info", "meta", "kh_meta"}
_PART_PREFIX = {"log": "kh_log_", "knowledge": "kh_know_", "findings": "kh_find_", "policy": "kh_policy_"}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _make_client():
    b = config.VECTOR_BACKEND
    if b == "hana":
        from app.vector_hana import HanaVectorClient

        return HanaVectorClient()
    if b == "chroma":
        try:
            import chromadb  # lazy, optional
            from chromadb.config import Settings

            return chromadb.PersistentClient(path=config.CHROMA_PATH,
                                             settings=Settings(anonymized_telemetry=False))
        except ImportError:
            log.warning("KH_VECTOR_BACKEND=chroma but chromadb is not installed; using the SQLite adapter")
    from app.vector_sqlite import SqliteVectorClient

    return SqliteVectorClient()


_client = _make_client()
_named_collections: dict[str, Any] = {}
_src_handles: "OrderedDict[str, Any]" = OrderedDict()
_lock = threading.Lock()

_collection = _client.get_or_create_collection(config.COLLECTION_NAME, metadata=COSINE)
_keys_collection = _client.get_or_create_collection(config.KEYS_COLLECTION_NAME, metadata=COSINE)


def client():
    return _client


def shared_collection():
    return _collection


def _name_of(c) -> str:
    return c if isinstance(c, str) else c.name


def list_collection_names() -> list[str]:
    return sorted(_name_of(c) for c in _client.list_collections())


def collection_exists(name: str) -> bool:
    has = getattr(_client, "has_collection", None)
    if callable(has):
        return bool(has(name))
    return name in list_collection_names()


def get_or_create(name: str):
    if name == config.COLLECTION_NAME:
        return _collection
    h = _named_collections.get(name)
    if h is not None:
        return h
    with _lock:
        h = _client.get_or_create_collection(name, metadata=COSINE)
        if name.startswith("kh_src_"):
            _src_handles[name] = h
            while len(_src_handles) > max(1, config.MAX_OPEN_COLLECTIONS):
                _src_handles.popitem(last=False)
        else:
            _named_collections[name] = h
    return h


def get_existing(name: str):
    """Handle of an existing collection, or None (reads never create collections)."""
    if name == config.COLLECTION_NAME:
        return _collection
    h = _named_collections.get(name) or _src_handles.get(name)
    if h is not None and collection_exists(name):
        return h
    if not collection_exists(name):
        _named_collections.pop(name, None)
        return None
    return get_or_create(name)


def drop_collection(name: str) -> bool:
    _named_collections.pop(name, None)
    _src_handles.pop(name, None)
    try:
        _client.delete_collection(name)
        return True
    except Exception:  # noqa: BLE001 - absent
        return False


def every_collection() -> list:
    """Shared collection + every existing collection (deletion by exact id only)."""
    out = [_collection]
    for n in list_collection_names():
        if n != config.COLLECTION_NAME:
            h = get_existing(n)
            if h is not None:
                out.append(h)
    return out


def reset_caches() -> None:
    _named_collections.clear()
    _src_handles.clear()


# ---------------------------------------------------------------------------
# Naming / write routing
# ---------------------------------------------------------------------------


def normalize_project(project: str | None) -> str | None:
    if project is None:
        return None
    p = str(project).strip()
    if not p:
        return None
    if p in ORCH_ALIASES:
        return ORCHESTRATOR
    return p


def own_project(project: str | None) -> str:
    return normalize_project(project) or ORCHESTRATOR


def is_personal(project: str | None) -> bool:
    return bool(project) and str(project).startswith(config.PERSONAL_PREFIX)


def part_collection_name(scope: str, project: str | None) -> str:
    pid = own_project(project)
    prefix = _PART_PREFIX.get(scope)
    if prefix:
        return prefix + pid
    return f"kh_{scope}_{pid}"


def scope_prefix(scope: str) -> str:
    return _PART_PREFIX.get(scope) or f"kh_{scope}_"


def scope_collection_name(scope: str | None, project: str | None) -> str | None:
    """Write routing (§1.7). None = the shared legacy collection."""
    if not scope:
        return None
    if scope in COMMON_SCOPES:
        if scope == config.PERSONAL_SCOPE and is_personal(project):
            return part_collection_name(scope, project)
        return part_collection_name("knowledge", ORCHESTRATOR)
    return part_collection_name(scope, own_project(project))


def scope_collection(scope: str | None, project: str | None):
    name = scope_collection_name(scope, project)
    return _collection if name is None else get_or_create(name)


def scope_collections_all(scope: str, visible: Iterable[str] | None = None) -> list[str]:
    """Existing per-project collection names of a scope among `visible` projects (None = all) + orchestrator."""
    prefix = scope_prefix(scope)
    vis = None if visible is None else set(visible)
    out = []
    for n in list_collection_names():
        if not n.startswith(prefix):
            continue
        pid = n[len(prefix):]
        if not pid:
            continue
        if vis is None or pid in vis or pid == ORCHESTRATOR:
            out.append(n)
    return out


def project_collections(pid: str) -> list[str]:
    """Every existing collection that belongs to project `pid` (kh_*_<pid>)."""
    suffix = "_" + pid
    out = []
    for n in list_collection_names():
        if not (n.startswith("kh_") and n.endswith(suffix)) or n == config.HOTLOG_COLLECTION:
            continue
        middle = n[3:len(n) - len(suffix)]
        if middle and "_" not in middle:  # kh_<scope>_<pid>; avoids kh_know_a_b matching project "b"
            out.append(n)
    return out


# ---------------------------------------------------------------------------
# Read routing
# ---------------------------------------------------------------------------


def and_where(*wheres: dict | None) -> dict | None:
    parts = [w for w in wheres if w]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    flat: list = []
    for w in parts:
        if set(w) == {"$and"}:
            flat.extend(w["$and"])
        else:
            flat.append(w)
    return {"$and": flat}


def _auth():
    from app import auth

    return auth


def read_collections(scope: str | None, project: str | None) -> list:
    """Collections a read of (scope, project) covers (no hot-window lane). Missing collections are skipped."""
    if not scope:
        return [_collection]
    project = normalize_project(project)
    if project and is_personal(project):
        h = get_existing(scope_collection_name(scope, project))
        return [h] if h is not None else []
    if scope in COMMON_SCOPES:
        out = []
        h = get_existing(part_collection_name("knowledge", ORCHESTRATOR))
        if h is not None:
            out.append(h)
        out.append(_collection)
        return out
    if project:
        h = get_existing(part_collection_name(scope, project))
        return [h] if h is not None else []
    visible = _auth().visible_projects("project.read")
    out = [get_existing(n) for n in scope_collections_all(scope, visible)]
    out = [h for h in out if h is not None]
    out.append(_collection)
    return out


def read_lanes(scope: str | None, project: str | None, where: dict | None = None) -> list[tuple[Any, dict | None]]:
    """[(collection, where)] per physical collection + the hot-window lane (§1.7 `_read_lanes`).

    Raises HTTPException 403 "missing permission project.read on this project" when the named project is
    not readable by the caller.
    """
    project = normalize_project(project)
    if project and not _auth().may(project, "project.read"):
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="missing permission project.read on this project")
    scope_where = and_where({"scope": {"$eq": scope}}, where) if scope else where
    lanes = [(h, scope_where) for h in read_collections(scope, project)]
    from app import hotlog

    hot = hotlog.read_lane(scope, project, scope_where)
    if hot is not None:
        lanes.append(hot)
    return lanes


# ---------------------------------------------------------------------------
# Row stamping and writes
# ---------------------------------------------------------------------------


def _stamp(meta: dict | None, vector: Sequence[float], text: str | None) -> dict:
    from app.embeddings import embedder_name

    m = {k: v for k, v in (meta or {}).items() if v is not None}
    if text is not None:
        m["source_text"] = text
    m.setdefault("emb_model", embedder_name())
    m.setdefault("emb_ver", config.KH_VERSION)
    m["emb_dim"] = len(vector)
    return m


def _vec(v) -> list:
    return v.tolist() if hasattr(v, "tolist") else list(v)


def upsert_rows(coll, ids: Sequence[str], vectors: Sequence, texts: Sequence[str] | None,
                metas: Sequence[dict] | None) -> None:
    vecs = [_vec(v) for v in vectors]
    texts = list(texts) if texts is not None else [None] * len(ids)
    metas = list(metas) if metas is not None else [{}] * len(ids)
    stamped = [_stamp(m, v, t) for m, v, t in zip(metas, vecs, texts)]
    docs = texts if all(t is not None for t in texts) else None
    coll.upsert(ids=list(ids), embeddings=vecs, documents=docs, metadatas=stamped)


def upsert_one(coll, row_id: str, vector, text: str, meta: dict) -> None:
    upsert_rows(coll, [row_id], [vector], [text], [meta])


# ---------------------------------------------------------------------------
# Query / enumeration primitives
# ---------------------------------------------------------------------------

_TRANSIENT = ("Nothing found on disk", "Error creating hnsw segment reader")


def _count(coll) -> int:
    try:
        return int(coll.count())
    except Exception:  # noqa: BLE001
        return 0


def query_vt(coll, vector, k: int, where: dict | None = None, where_document: dict | None = None) -> list[dict]:
    """Top-k by cosine. Hit = {vector_id, score, source_text, metadata}. [] on an empty collection."""
    n_all = _count(coll)
    if n_all <= 0:
        return []
    n = max(1, min(int(k), n_all))
    vec = _vec(vector)
    delays = (0.05, 0.10, 0.15)
    for attempt in range(4):
        try:
            res = coll.query(query_embeddings=[vec], n_results=n, where=where or None,
                             where_document=where_document or None,
                             include=["metadatas", "documents", "distances"])
            break
        except Exception as e:  # noqa: BLE001
            if attempt < 3 and any(t in str(e) for t in _TRANSIENT):
                time.sleep(delays[attempt])
                continue
            raise
    ids = (res.get("ids") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0] or [{}] * len(ids)
    docs = (res.get("documents") or [[]])[0] or [None] * len(ids)
    out = []
    for i, rid in enumerate(ids):
        meta = metas[i] or {}
        out.append({
            "vector_id": rid,
            "score": round(1.0 - float(dists[i]), 6),
            "source_text": docs[i] if docs[i] is not None else meta.get("source_text"),
            "metadata": meta,
        })
    return out


def _rows(res: dict) -> list[dict]:
    ids = res.get("ids") or []
    docs = res.get("documents") or [None] * len(ids)
    metas = res.get("metadatas") or [{}] * len(ids)
    out = []
    for i, rid in enumerate(ids):
        meta = metas[i] or {}
        out.append({"vector_id": rid, "source_text": docs[i] if docs[i] is not None else meta.get("source_text"),
                    "metadata": meta})
    return out


def get_vt(coll, where: dict | None = None, limit: int | None = None, offset: int | None = None,
           where_document: dict | None = None) -> list[dict]:
    """Rows without similarity: [{vector_id, source_text, metadata}] in insertion order."""
    res = coll.get(where=where or None, limit=limit, offset=offset, where_document=where_document or None,
                   include=["metadatas", "documents"])
    return _rows(res)


def get_ids_vt(coll, ids: Sequence[str], where: dict | None = None) -> list[dict]:
    """Exact ids still inside a lane's filter, in request order."""
    if not ids:
        return []
    res = coll.get(ids=list(ids), where=where or None, include=["metadatas", "documents"])
    return _rows(res)


def meta_rows_vt(coll, where: dict | None = None) -> list[tuple[str, dict]]:
    """[(id, metadata)] without reading text (the metadata copy `source_text` is dropped)."""
    res = coll.get(where=where or None, include=["metadatas"])
    out = []
    for rid, m in zip(res.get("ids") or [], res.get("metadatas") or []):
        m = dict(m or {})
        m.pop("source_text", None)
        out.append((rid, m))
    return out


def field_values_vt(coll, field: str, where: dict | None = None) -> list[tuple[str, Any]]:
    return [(rid, m.get(field)) for rid, m in meta_rows_vt(coll, where)]


def scroll_vt(coll, where: dict | None = None, limit: int = 500, offset: int = 0) -> dict:
    """{matched (true total), count, offset, limit, hits:[{vector_id, source_text, metadata}]}."""
    if where:
        matched = len(coll.get(where=where, include=[]).get("ids") or [])
    else:
        matched = _count(coll)
    hits = get_vt(coll, where=where, limit=limit, offset=offset)
    return {"matched": matched, "count": len(hits), "offset": offset, "limit": limit, "hits": hits}


def topics_vt(coll, field: str = "topic", where: dict | None = None,
              keep: Callable[[dict, str], bool] | None = None) -> dict:
    counts: dict[Any, int] = {}
    total = missing = 0
    for rid, m in meta_rows_vt(coll, where):
        if keep is not None and not keep(m, rid):
            continue
        total += 1
        v = m.get(field)
        if v is None or v == "":
            missing += 1
            continue
        counts[v] = counts.get(v, 0) + 1
    return {"field": field, "distinct": len(counts), "total_scanned": total, "missing_field": missing,
            "topics": topics_sorted(counts)}


def topics_sorted(counts: dict) -> list[dict]:
    return [{"value": v, "count": n} for v, n in sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))]


_TOPIC_RE = re.compile(r"topic:([A-Za-z0-9_\-]+)")


def backfill_topic_vt(coll, where: dict | None = None, field: str = "topic", apply: bool = False) -> dict:
    rows = get_vt(coll, where=where)
    changes = []
    for r in rows:
        m = re.search(_TOPIC_RE, r["source_text"] or "")
        if not m:
            continue
        want = m.group(1)
        have = (r["metadata"] or {}).get(field)
        if have != want:
            changes.append({"vector_id": r["vector_id"], "from": have, "to": want})
    applied = 0
    if apply and changes:
        by_id = {r["vector_id"]: r for r in rows}
        ids, metas = [], []
        for ch in changes:
            meta = dict(by_id[ch["vector_id"]]["metadata"] or {})
            meta[field] = ch["to"]
            ids.append(ch["vector_id"])
            metas.append(meta)
        coll.update(ids=ids, metadatas=metas)
        applied = len(ids)
    return {"scanned": len(rows), "mismatched": len(changes), "applied": applied, "changes": changes}


def fill_missing_field_vt(coll, where: dict | None, field: str, value: Any, apply: bool = False,
                          batch: int = 500) -> dict:
    """Set `field` only where empty. Returns {scanned, missing, applied}."""
    res = coll.get(where=where or None, include=["metadatas"])
    ids = res.get("ids") or []
    metas = res.get("metadatas") or []
    todo = [(rid, dict(m or {})) for rid, m in zip(ids, metas) if (m or {}).get(field) in (None, "")]
    applied = 0
    if apply:
        for i in range(0, len(todo), batch):
            part = todo[i:i + batch]
            for _, m in part:
                m[field] = value
            coll.update(ids=[r for r, _ in part], metadatas=[m for _, m in part])
            applied += len(part)
    return {"scanned": len(ids), "missing": len(todo), "applied": applied}


def delete_vt(ids: Sequence[str], coll) -> list[str]:
    """Delete only ids that exist; returns them."""
    if not ids:
        return []
    found = coll.get(ids=list(ids), include=[]).get("ids") or []
    if found:
        coll.delete(ids=list(found))
    return list(found)


def move_rows(ids: Sequence[str], src, dst) -> int:
    """Copy (with embeddings, no re-encoding) then delete. A crash leaves a duplicate, never a loss."""
    if not ids:
        return 0
    res = src.get(ids=list(ids), include=["embeddings", "documents", "metadatas"])
    got = res.get("ids") or []
    if not got:
        return 0
    embs = res.get("embeddings")
    dst.upsert(ids=got, embeddings=[_vec(e) for e in embs], documents=res.get("documents"),
               metadatas=res.get("metadatas"))
    src.delete(ids=got)
    return len(got)


# ---------------------------------------------------------------------------
# Canonical keys (§1.7, /keys/*)
# ---------------------------------------------------------------------------


def keys_collection(project: str | None = None):
    p = normalize_project(project)
    if not p or p == ORCHESTRATOR:
        return _keys_collection
    return get_or_create(f"kh_keys_{p}")


def _dedupe_ci(items: Iterable[str]) -> list[str]:
    seen, out = set(), []
    for s in items:
        s = (s or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def keys_add(key: str, aliases: Sequence[str] = (), merge: bool = True, metadata: dict | None = None,
             project: str | None = None) -> dict:
    from app.embeddings import get_embedder

    coll = keys_collection(project)
    forms = [key]
    if merge:
        res = coll.get(ids=[key], include=["metadatas", "documents"])
        if res.get("ids"):
            stored = (res["documents"] or [None])[0] or (res["metadatas"][0] or {}).get("aliases") or ""
            forms.extend(s for s in stored.split(" | "))
    forms.extend(aliases or [])
    forms = _dedupe_ci([key] + forms[1:])
    joined = " | ".join(forms)
    vec = get_embedder().encode(joined, normalize_embeddings=True)
    meta = dict(metadata or {})
    meta.update({"kind": "canonical_key", "canonical_key": key, "aliases": joined})
    upsert_one(coll, key, vec, joined, meta)
    return {"key": key, "aliases": [f for f in forms if f != key], "dimension": len(vec)}


def keys_query(text: str, k: int = 5, project: str | None = None) -> list[dict]:
    from app.embeddings import get_embedder

    vec = get_embedder().encode(text, normalize_embeddings=True)
    hits = query_vt(keys_collection(project), vec, k)
    out = []
    for h in hits:
        key = h["metadata"].get("canonical_key") or h["vector_id"]
        forms = (h["source_text"] or "").split(" | ")
        out.append({"key": key, "score": h["score"], "aliases": [f for f in forms if f and f != key],
                    "source_text": h["source_text"]})
    return out


def keys_delete(keys: Sequence[str], project: str | None = None) -> dict:
    coll = keys_collection(project)
    deleted = delete_vt(list(dict.fromkeys(keys)), coll)
    return {"status": "ok", "requested": len(keys), "deleted": deleted,
            "not_found": [k for k in dict.fromkeys(keys) if k not in set(deleted)]}


# ---------------------------------------------------------------------------
# Legacy orchestrator source lanes (spec §1.7 "Legacy orchestrator lanes", LEGACY)
# ---------------------------------------------------------------------------

SRC_PREFIX = "kh_src_"
HUB_COLLECTION = "kh_orch_hub"
SRC_UPSERT_CHUNK = 1000
KEYWORD_MAX_TERMS = 8
KEYWORD_MIN_LEN = 3
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")
# Small English stop list (the original word list is UNKNOWN; see docs/deviations.md).
KEYWORD_STOP = frozenset("""
the and for with from that this these those what where when which who whom whose how why into onto
are was were been being has have had not but all any can does did done you your our its about there
their then than them they will would should could shall may might must also only just very more most
some such each other over under upon out off per via use used using get gets got let lets
""".split())


def src_collection_name(source: str) -> str:
    return f"{SRC_PREFIX}{source}"


def src_collection(source: str):
    """`kh_src_<source>` handle (LRU-bounded to KH_MAX_OPEN_COLLECTIONS; created on demand)."""
    return get_or_create(src_collection_name(source))


def upsert_src_batch(source: str, ids: Sequence[str], vectors: Sequence, texts: Sequence[str] | None = None,
                     metas: Sequence[dict] | None = None) -> int:
    """Upsert rows into `kh_src_<source>` in chunks of 1000. Documents are passed for a chunk only when every
    text in it is non-empty (else the text survives in metadata.source_text). Returns rows written."""
    ids = list(ids)
    if not ids:
        return 0
    vecs = [_vec(v) for v in vectors]
    texts = list(texts) if texts is not None else [None] * len(ids)
    metas = list(metas) if metas is not None else [{}] * len(ids)
    if not (len(vecs) == len(texts) == len(metas) == len(ids)):
        raise ValueError("ids, vectors, texts and metas must have the same length")
    coll = src_collection(source)
    for i in range(0, len(ids), SRC_UPSERT_CHUNK):
        sl = slice(i, i + SRC_UPSERT_CHUNK)
        c_ids, c_vecs, c_texts, c_metas = ids[sl], vecs[sl], texts[sl], metas[sl]
        stamped = [_stamp(m, v, t or None) for m, v, t in zip(c_metas, c_vecs, c_texts)]
        docs = list(c_texts) if all(isinstance(t, str) and t for t in c_texts) else None
        coll.upsert(ids=list(c_ids), embeddings=c_vecs, documents=docs, metadatas=stamped)
    return len(ids)


def upsert_src(source: str, row_id: str, vector, text: str | None, meta: dict | None = None) -> int:
    return upsert_src_batch(source, [row_id], [vector], [text], [meta or {}])


def _src_handle(source: str, fresh: bool = False):
    name = src_collection_name(source)
    if fresh:
        _src_handles.pop(name, None)
    return get_existing(name)


def query_src(source: str, vector, k: int = 10, where: dict | None = None) -> list[dict]:
    """Vector lane over `kh_src_<source>`. On error: retry once with a fresh handle, then degrade to []."""
    for attempt in (0, 1):
        try:
            coll = _src_handle(source, fresh=attempt > 0)
            if coll is None:
                return []
            return query_vt(coll, vector, k, where=where)
        except Exception as e:  # noqa: BLE001
            if attempt == 0:
                log.info("query_src(%s) failed (%s); retrying with a fresh handle", source, e)
                continue
            log.warning("query_src(%s) failed twice (%s); vector lane degraded to []", source, e)
    return []


def keyword_terms(text: str) -> list[str]:
    """Identifier-ish terms of a query: regex `[A-Za-z_][A-Za-z0-9_]{1,}`, len >= 3, not a stop word,
    first-seen order, at most 8."""
    out: list[str] = []
    for t in _IDENT_RE.findall(text or ""):
        if len(t) < KEYWORD_MIN_LEN or t.lower() in KEYWORD_STOP or t in out:
            continue
        out.append(t)
        if len(out) >= KEYWORD_MAX_TERMS:
            break
    return out


def keyword_where_document(text: str) -> dict | None:
    terms = keyword_terms(text)
    if not terms:
        return None
    if len(terms) == 1:
        return {"$contains": terms[0]}
    return {"$or": [{"$contains": t} for t in terms]}


def keyword_src(source: str, text: str, k: int = 10, where: dict | None = None) -> list[dict]:
    """Lexical lane over `kh_src_<source>`: rows whose document contains any query term (case-sensitive
    substring). Hits `{vector_id, score: None, source_text, metadata, matched_terms}`, in store order."""
    terms = keyword_terms(text)
    wd = keyword_where_document(text)
    if wd is None:
        return []
    try:
        coll = _src_handle(source)
        if coll is None:
            return []
        rows = get_vt(coll, where=where, limit=max(1, int(k)), where_document=wd)
    except Exception as e:  # noqa: BLE001
        log.warning("keyword_src(%s) failed (%s); keyword lane degraded to []", source, e)
        return []
    for r in rows:
        doc = r.get("source_text") or ""
        r["score"] = None
        r["matched_terms"] = [t for t in terms if t in doc]
    return rows


def _core_id(hit: dict) -> str:
    meta = hit.get("metadata") or {}
    return str(meta.get("core_id") or hit.get("vector_id"))


def hybrid_src(source: str, text: str, vector, k: int = 10, where: dict | None = None) -> list[dict]:
    """Vector + keyword candidates merged by core_id (metadata.core_id, else the row id). Each candidate
    `{core_id, vector_id, score, source_text, metadata, lanes, matched_terms}`: vector hits first in score
    order, then keyword-only hits in store order. No re-ranking: the calling agent judges."""
    merged: "OrderedDict[str, dict]" = OrderedDict()
    vec_hits = query_src(source, vector, k, where) if vector is not None else []
    for lane, hits in (("vector", vec_hits), ("keyword", keyword_src(source, text, k, where))):
        for h in hits:
            cid = _core_id(h)
            cand = merged.get(cid)
            if cand is None:
                cand = merged[cid] = {"core_id": cid, "vector_id": h.get("vector_id"), "score": h.get("score"),
                                      "source_text": h.get("source_text"), "metadata": h.get("metadata") or {},
                                      "lanes": [], "matched_terms": []}
            if lane not in cand["lanes"]:
                cand["lanes"].append(lane)
            if cand["score"] is None and h.get("score") is not None:
                cand["score"] = h["score"]
            for t in h.get("matched_terms") or []:
                if t not in cand["matched_terms"]:
                    cand["matched_terms"].append(t)
    return list(merged.values())


def hub_upsert(source: str, vector, text: str, meta: dict | None = None) -> None:
    """Hub descriptor of a source: one row in `kh_orch_hub`, id = source id."""
    m = {**(meta or {}), "source_id": source}
    m.setdefault("kind", "source-descriptor")
    upsert_one(get_or_create(HUB_COLLECTION), source, vector, text, m)


def hub_get(source: str) -> dict | None:
    coll = get_existing(HUB_COLLECTION)
    if coll is None:
        return None
    rows = get_ids_vt(coll, [source])
    return rows[0] if rows else None


def hub_query(vector, k: int = 5, where: dict | None = None) -> list[dict]:
    coll = get_existing(HUB_COLLECTION)
    if coll is None:
        return []
    try:
        return query_vt(coll, vector, k, where=where)
    except Exception as e:  # noqa: BLE001
        log.warning("hub_query failed (%s); degraded to []", e)
        return []


def hub_delete(source: str) -> list[str]:
    coll = get_existing(HUB_COLLECTION)
    return delete_vt([source], coll) if coll is not None else []
