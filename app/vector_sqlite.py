"""SQLite vector adapter (rebuild contract; default `KH_VECTOR_BACKEND=sqlite`).

Same design as the HANA adapter (§1.7.1) on SQLite: tables KH_VEC_COLLECTIONS / KH_VEC_ROWS in the hooks DB,
embeddings as float32 blobs, cosine in numpy, full Chroma `where` / `where_document` semantics in Python
(string $eq/$in prefiltered in SQL via json_extract). Exposes the Chroma client/collection API subset KH uses.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Sequence

import numpy as np

from app import config, sqldb
from app.vector_filter import matches_document, matches_where, sql_filter

COLL_T = "KH_VEC_COLLECTIONS"
ROWS_T = "KH_VEC_ROWS"
_CHUNK = 500


def _jv(key: str) -> str:
    return f"json_extract(META, '$.{key}')"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _blob(vec) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def _unblob(b) -> np.ndarray | None:
    if b is None:
        return None
    return np.frombuffer(b, dtype=np.float32)


def _dumps(meta) -> str:
    return json.dumps(meta or {}, ensure_ascii=False)


def _loads(s) -> dict:
    if not s:
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


class SqliteCollection:
    def __init__(self, client: "SqliteVectorClient", name: str, metadata: dict | None = None):
        self._client = client
        self.name = name
        self.metadata = metadata or {"hnsw:space": "cosine"}

    def __repr__(self):
        return f"SqliteCollection({self.name!r})"

    def _conn(self):
        return self._client._conn()

    # -- reads -------------------------------------------------------------
    def count(self) -> int:
        with self._conn() as c:
            return int(c.execute(f"SELECT COUNT(*) FROM {ROWS_T} WHERE COLL=?", (self.name,)).fetchone()[0])

    def _select(self, c, cols: str, ids: Sequence[str] | None, where, where_document, extra: str = ""):
        """Yield raw rows (with META parsed and filters applied) in SEQ order (or request order for ids)."""
        fsql, fparams, exact = sql_filter(where, _jv)
        need_py = (not exact) or bool(where_document)
        rows: list = []
        if ids is not None:
            ids = list(ids)
            for i in range(0, len(ids), _CHUNK):
                part = ids[i:i + _CHUNK]
                q = (f"SELECT {cols} FROM {ROWS_T} WHERE COLL=? AND ID IN ({', '.join('?' for _ in part)})"
                     f"{fsql}{extra}")
                rows.extend(c.execute(q, [self.name, *part, *fparams]).fetchall())
            order = {v: n for n, v in enumerate(ids)}
            rows.sort(key=lambda r: order.get(r["ID"], 0))
        else:
            q = f"SELECT {cols} FROM {ROWS_T} WHERE COLL=?{fsql}{extra} ORDER BY SEQ"
            rows = c.execute(q, [self.name, *fparams]).fetchall()
        out = []
        for r in rows:
            meta = _loads(r["META"])
            if need_py:
                if where and not matches_where(meta, where):
                    continue
                if where_document and not matches_document(r["DOC"], where_document):
                    continue
            out.append((r, meta))
        return out

    def get(self, ids: Sequence[str] | str | None = None, where: dict | None = None, limit: int | None = None,
            offset: int | None = None, where_document: dict | None = None,
            include: Sequence[str] | None = None) -> dict:
        include = list(include) if include is not None else ["metadatas", "documents"]
        if isinstance(ids, str):
            ids = [ids]
        want_emb = "embeddings" in include
        cols = "ID, META, DOC" + (", EMB" if want_emb else "")
        with self._conn() as c:
            filtered = bool(where) or bool(where_document)
            if ids is None and not filtered and (limit is not None or offset):
                extra = f" ORDER BY SEQ LIMIT {int(limit) if limit is not None else -1} OFFSET {int(offset or 0)}"
                raw = c.execute(f"SELECT {cols} FROM {ROWS_T} WHERE COLL=?{extra}", (self.name,)).fetchall()
                items = [(r, _loads(r["META"])) for r in raw]
            else:
                items = self._select(c, cols, ids, where, where_document)
                if offset:
                    items = items[int(offset):]
                if limit is not None:
                    items = items[:int(limit)]
        return {
            "ids": [r["ID"] for r, _ in items],
            "embeddings": [(_unblob(r["EMB"]).tolist() if r["EMB"] is not None else None) for r, _ in items]
            if want_emb else None,
            "documents": [r["DOC"] for r, _ in items] if "documents" in include else None,
            "metadatas": [m for _, m in items] if "metadatas" in include else None,
        }

    def query(self, query_embeddings=None, n_results: int = 10, where: dict | None = None,
              where_document: dict | None = None, include: Sequence[str] | None = None,
              query_texts=None) -> dict:
        if query_embeddings is None:
            raise ValueError("query_embeddings is required")
        include = list(include) if include is not None else ["metadatas", "documents", "distances"]
        qs = np.asarray(query_embeddings, dtype=np.float32)
        if qs.ndim == 1:
            qs = qs[None, :]
        want_emb = "embeddings" in include
        with self._conn() as c:
            items = self._select(c, "ID, META, DOC, EMB", None, where, where_document, " AND EMB IS NOT NULL")
        res = {"ids": [], "distances": [], "metadatas": [], "documents": [], "embeddings": [] if want_emb else None}
        if items:
            mat = []
            keep = []
            for r, m in items:
                v = _unblob(r["EMB"])
                if v is not None and v.shape[0] == qs.shape[1]:
                    mat.append(v)
                    keep.append((r, m))
            items = keep
            M = np.stack(mat) if mat else np.zeros((0, qs.shape[1]), dtype=np.float32)
            norms = np.linalg.norm(M, axis=1) if len(M) else np.zeros(0)
        for q in qs:
            if not items:
                for k in ("ids", "distances", "metadatas", "documents"):
                    res[k].append([])
                if want_emb:
                    res["embeddings"].append([])
                continue
            qn = float(np.linalg.norm(q)) or 1.0
            denom = norms * qn
            denom[denom == 0] = 1.0
            sims = (M @ q) / denom
            n = max(0, min(int(n_results), len(items)))
            order = np.argsort(-sims, kind="stable")[:n]
            res["ids"].append([items[i][0]["ID"] for i in order])
            res["distances"].append([round(1.0 - float(sims[i]), 9) for i in order])
            res["metadatas"].append([items[i][1] for i in order])
            res["documents"].append([items[i][0]["DOC"] for i in order])
            if want_emb:
                res["embeddings"].append([M[i].tolist() for i in order])
        if "metadatas" not in include:
            res["metadatas"] = None
        if "documents" not in include:
            res["documents"] = None
        return res

    # -- writes ------------------------------------------------------------
    def _write(self, mode: str, ids, embeddings=None, documents=None, metadatas=None) -> None:
        if isinstance(ids, str):
            ids = [ids]
            embeddings = [embeddings] if embeddings is not None and np.ndim(embeddings) == 1 else embeddings
            documents = [documents] if isinstance(documents, str) else documents
            metadatas = [metadatas] if isinstance(metadatas, dict) else metadatas
        ids = list(ids)
        n = len(ids)
        for name, arr in (("embeddings", embeddings), ("documents", documents), ("metadatas", metadatas)):
            if arr is not None and len(arr) != n:
                raise ValueError(f"{name} length {len(arr)} != ids length {n}")
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._client._ensure_row(c, self.name, self.metadata)
            existing: set = set()
            for i in range(0, n, _CHUNK):
                part = ids[i:i + _CHUNK]
                existing.update(r[0] for r in c.execute(
                    f"SELECT ID FROM {ROWS_T} WHERE COLL=? AND ID IN ({', '.join('?' for _ in part)})",
                    [self.name, *part]).fetchall())
            seq = int(c.execute(f"SELECT COALESCE(MAX(SEQ), 0) FROM {ROWS_T}").fetchone()[0])
            last = {rid: j for j, rid in enumerate(ids)}  # duplicate id within one call: last wins
            inserts = []
            for rid, j in last.items():
                emb = embeddings[j] if embeddings is not None else None
                doc = documents[j] if documents is not None else None
                meta = metadatas[j] if metadatas is not None else None
                if rid in existing:
                    if mode == "add":
                        continue
                    sets, params = [], []
                    if emb is not None:
                        sets.append("EMB=?")
                        params.append(_blob(emb))
                    if documents is not None:
                        sets.append("DOC=?")
                        params.append(doc)
                    if metadatas is not None:
                        sets.append("META=?")
                        params.append(_dumps(meta))
                    if sets:
                        c.execute(f"UPDATE {ROWS_T} SET {', '.join(sets)} WHERE COLL=? AND ID=?",
                                  [*params, self.name, rid])
                    continue
                if mode == "update":
                    continue
                if emb is None:
                    raise ValueError(f"inserting row {rid!r} requires an embedding")
                seq += 1
                inserts.append((self.name, rid, seq, _blob(emb), doc, _dumps(meta)))
            if inserts:
                c.executemany(f"INSERT INTO {ROWS_T} (COLL, ID, SEQ, EMB, DOC, META) VALUES (?, ?, ?, ?, ?, ?)",
                              inserts)

    def add(self, ids, embeddings=None, documents=None, metadatas=None, **_):
        self._write("add", ids, embeddings, documents, metadatas)

    def upsert(self, ids, embeddings=None, documents=None, metadatas=None, **_):
        self._write("upsert", ids, embeddings, documents, metadatas)

    def update(self, ids, embeddings=None, documents=None, metadatas=None, **_):
        self._write("update", ids, embeddings, documents, metadatas)

    def delete(self, ids=None, where: dict | None = None, where_document: dict | None = None) -> list:
        if isinstance(ids, str):
            ids = [ids]
        if where or where_document:
            ids = self.get(ids=ids, where=where, where_document=where_document, include=[])["ids"]
        if ids is None:
            return []
        ids = list(ids)
        with self._conn() as c:
            for i in range(0, len(ids), _CHUNK):
                part = ids[i:i + _CHUNK]
                c.execute(f"DELETE FROM {ROWS_T} WHERE COLL=? AND ID IN ({', '.join('?' for _ in part)})",
                          [self.name, *part])
        return ids


class SqliteVectorClient:
    def __init__(self, path: str | None = None):
        self.path = path or config.HOOKS_DB_PATH
        self._lock = threading.Lock()
        self.bootstrap()

    def _conn(self):
        return sqldb.sqlite_connect(self.path)

    def bootstrap(self) -> None:
        with self._conn() as c:
            c.execute(f"CREATE TABLE IF NOT EXISTS {COLL_T} (NAME TEXT PRIMARY KEY, META TEXT, CREATED_AT TEXT)")
            c.execute(f"CREATE TABLE IF NOT EXISTS {ROWS_T} (COLL TEXT NOT NULL, ID TEXT NOT NULL, SEQ INTEGER, "
                      f"EMB BLOB, DOC TEXT, META TEXT, PRIMARY KEY (COLL, ID))")
            c.execute(f"CREATE INDEX IF NOT EXISTS IX_KH_VEC_ROWS_SEQ ON {ROWS_T} (COLL, SEQ)")

    def _ensure_row(self, c, name: str, metadata: dict | None) -> None:
        c.execute(f"INSERT OR IGNORE INTO {COLL_T} (NAME, META, CREATED_AT) VALUES (?, ?, ?)",
                  (name, _dumps(metadata or {"hnsw:space": "cosine"}), _now_iso()))

    def get_or_create_collection(self, name: str, metadata: dict | None = None, **_) -> SqliteCollection:
        with self._conn() as c:
            self._ensure_row(c, name, metadata)
            r = c.execute(f"SELECT META FROM {COLL_T} WHERE NAME=?", (name,)).fetchone()
        return SqliteCollection(self, name, _loads(r[0]) if r else metadata)

    def create_collection(self, name: str, metadata: dict | None = None, **_) -> SqliteCollection:
        if self.has_collection(name):
            raise ValueError(f"Collection {name} already exists")
        return self.get_or_create_collection(name, metadata)

    def get_collection(self, name: str, **_) -> SqliteCollection:
        with self._conn() as c:
            r = c.execute(f"SELECT META FROM {COLL_T} WHERE NAME=?", (name,)).fetchone()
        if r is None:
            raise ValueError(f"Collection {name} does not exist.")
        return SqliteCollection(self, name, _loads(r[0]))

    def has_collection(self, name: str) -> bool:
        with self._conn() as c:
            return c.execute(f"SELECT 1 FROM {COLL_T} WHERE NAME=?", (name,)).fetchone() is not None

    def list_collections(self) -> list[SqliteCollection]:
        with self._conn() as c:
            rows = c.execute(f"SELECT NAME, META FROM {COLL_T} ORDER BY NAME").fetchall()
        return [SqliteCollection(self, r[0], _loads(r[1])) for r in rows]

    def delete_collection(self, name: str) -> None:
        with self._conn() as c:
            cur = c.execute(f"DELETE FROM {COLL_T} WHERE NAME=?", (name,))
            if not cur.rowcount:
                raise ValueError(f"Collection {name} does not exist.")
            c.execute(f"DELETE FROM {ROWS_T} WHERE COLL=?", (name,))

    def reset(self) -> None:
        """Tests: wipe every collection."""
        with self._conn() as c:
            c.execute(f"DELETE FROM {ROWS_T}")
            c.execute(f"DELETE FROM {COLL_T}")
