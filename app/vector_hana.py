"""HANA vector adapter (spec §1.7.1). Chroma client/collection API on KH_VEC_COLLECTIONS / KH_VEC_ROWS.

Uses the store pool of app/hana_conn.py (hdbcli imported lazily). Not exercised by the test suite.
"""
from __future__ import annotations

import json
import logging
import time
from app import config, sqldb
from app.vector_filter import matches_document, matches_where, sql_filter

log = logging.getLogger(__name__)

COLL_T = config.HANA_VEC_COLLECTIONS_TABLE
ROWS_T = config.HANA_VEC_ROWS_TABLE
_CHUNK = 500
_json_filter_failed = False  # set once if HANA rejects a JSON_VALUE filter; Python matching from then on


def _jv(key: str) -> str:
    return f"JSON_VALUE(META, '$.{key}')"


def vec_literal(vec) -> str:
    return "[" + ",".join(format(float(x), ".9g") for x in vec) + "]"


def _loads(s) -> dict:
    if not s:
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _parse_vec(s):
    if s is None:
        return None
    try:
        return [float(x) for x in json.loads(s)]
    except (ValueError, TypeError):
        return None


def _conn():
    from app.hana_conn import store_pool

    return sqldb._HanaConn(store_pool())


class HanaCollection:
    def __init__(self, client: "HanaVectorClient", name: str, metadata: dict | None = None):
        self._client = client
        self.name = name
        self.metadata = metadata or {"hnsw:space": "cosine"}

    def count(self) -> int:
        with _conn() as c:
            return int(c.execute(f"SELECT COUNT(*) FROM {ROWS_T} WHERE COLL=?", (self.name,)).fetchone()[0])

    def _filter(self, where):
        global _json_filter_failed
        if _json_filter_failed:
            return "", [], False
        return sql_filter(where, _jv)

    def _run(self, c, sql_tpl: str, params_head: list, where, extra_params: list | None = None):
        """Run a query with the JSON prefilter; on HANA rejecting JSON_VALUE fall back to Python forever."""
        global _json_filter_failed
        fsql, fparams, exact = self._filter(where)
        try:
            rows = c.execute(sql_tpl.format(f=fsql), [*params_head, *fparams, *(extra_params or [])]).fetchall()
        except Exception as e:  # noqa: BLE001
            if not fsql:
                raise
            log.warning("HANA rejected JSON_VALUE filter (%s); switching to Python matching", e)
            _json_filter_failed = True
            exact = False
            rows = c.execute(sql_tpl.format(f=""), [*params_head, *(extra_params or [])]).fetchall()
        return rows, exact

    def get(self, ids=None, where=None, limit=None, offset=None, where_document=None, include=None) -> dict:
        include = list(include) if include is not None else ["metadatas", "documents"]
        if isinstance(ids, str):
            ids = [ids]
        want_emb = "embeddings" in include
        cols = "ID, META, DOC" + (", TO_NVARCHAR(EMB) AS EMB" if want_emb else "")
        items = []
        with _conn() as c:
            filtered = bool(where) or bool(where_document)
            if ids is not None:
                ids = list(ids)
                raw = []
                exact = True
                for i in range(0, len(ids), _CHUNK):
                    part = ids[i:i + _CHUNK]
                    tpl = (f"SELECT {cols} FROM {ROWS_T} WHERE COLL=? AND ID IN ({', '.join('?' for _ in part)})"
                           "{f}")
                    r, exact = self._run(c, tpl, [self.name, *part], where)
                    raw.extend(r)
                order = {v: n for n, v in enumerate(ids)}
                raw.sort(key=lambda r: order.get(r["id"], 0))
            elif not filtered:
                lim = f" LIMIT {int(limit)}" if limit is not None else (" LIMIT 2147483647" if offset else "")
                off = f" OFFSET {int(offset)}" if offset else ""
                raw = c.execute(f"SELECT {cols} FROM {ROWS_T} WHERE COLL=? ORDER BY SEQ{lim}{off}",
                                (self.name,)).fetchall()
                exact = True
                limit = offset = None
            else:
                raw, exact = self._run(c, f"SELECT {cols} FROM {ROWS_T} WHERE COLL=?{{f}} ORDER BY SEQ",
                                       [self.name], where)
        for r in raw:
            meta = _loads(r["meta"])
            if (not exact and where and not matches_where(meta, where)) or \
                    (where_document and not matches_document(r["doc"], where_document)):
                continue
            items.append((r, meta))
        if offset:
            items = items[int(offset):]
        if limit is not None:
            items = items[:int(limit)]
        return {
            "ids": [r["id"] for r, _ in items],
            "embeddings": [_parse_vec(r["emb"]) for r, _ in items] if want_emb else None,
            "documents": [r["doc"] for r, _ in items] if "documents" in include else None,
            "metadatas": [m for _, m in items] if "metadatas" in include else None,
        }

    def query(self, query_embeddings=None, n_results=10, where=None, where_document=None, include=None,
              **_) -> dict:
        include = list(include) if include is not None else ["metadatas", "documents", "distances"]
        qs = [list(map(float, q)) for q in query_embeddings] if query_embeddings is not None and             len(query_embeddings) and hasattr(query_embeddings[0], "__len__") else [list(map(float, query_embeddings))]
        res = {"ids": [], "distances": [], "metadatas": [], "documents": [], "embeddings": None}
        n = max(1, int(n_results))
        for q in qs:
            lit = vec_literal(q)
            with _conn() as c:
                fsql, fparams, exact = self._filter(where)
                if exact and not where_document:
                    sql = (f"SELECT TOP {n} ID, COSINE_SIMILARITY(EMB, TO_REAL_VECTOR(?)) AS SIM, META, DOC "
                           f"FROM {ROWS_T} WHERE COLL=? AND EMB IS NOT NULL{fsql} ORDER BY SIM DESC")
                    try:
                        rows = c.execute(sql, [lit, self.name, *fparams]).fetchall()
                        hits = [(r["id"], float(r["sim"]), _loads(r["meta"]), r["doc"]) for r in rows]
                    except Exception:  # noqa: BLE001
                        if not fsql:
                            raise
                        globals()["_json_filter_failed"] = True
                        return self.query(query_embeddings, n_results, where, where_document, include)
                else:
                    survivors = self.get(where=where, where_document=where_document, include=[])["ids"]
                    hits = []
                    for i in range(0, len(survivors), _CHUNK):
                        part = survivors[i:i + _CHUNK]
                        sql = (f"SELECT TOP {n} ID, COSINE_SIMILARITY(EMB, TO_REAL_VECTOR(?)) AS SIM, META, DOC "
                               f"FROM {ROWS_T} WHERE COLL=? AND EMB IS NOT NULL AND ID IN "
                               f"({', '.join('?' for _ in part)}) ORDER BY SIM DESC")
                        rows = c.execute(sql, [lit, self.name, *part]).fetchall()
                        hits.extend((r["id"], float(r["sim"]), _loads(r["meta"]), r["doc"]) for r in rows)
                    hits.sort(key=lambda h: -h[1])
                    hits = hits[:n]
            res["ids"].append([h[0] for h in hits])
            res["distances"].append([round(1.0 - h[1], 9) for h in hits])
            res["metadatas"].append([h[2] for h in hits])
            res["documents"].append([h[3] for h in hits])
        return res

    def _write(self, mode: str, ids, embeddings=None, documents=None, metadatas=None):
        if isinstance(ids, str):
            ids = [ids]
            embeddings = [embeddings] if embeddings is not None else None
            documents = [documents] if documents is not None else None
            metadatas = [metadatas] if metadatas is not None else None
        ids = list(ids)
        last = {rid: j for j, rid in enumerate(ids)}
        with _conn() as c:
            self._client._ensure_row(c, self.name, self.metadata)
            existing = set()
            for i in range(0, len(ids), _CHUNK):
                part = ids[i:i + _CHUNK]
                existing.update(r[0] for r in c.execute(
                    f"SELECT ID FROM {ROWS_T} WHERE COLL=? AND ID IN ({', '.join('?' for _ in part)})",
                    [self.name, *part]).fetchall())
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
                        sets.append("EMB=TO_REAL_VECTOR(?)")
                        params.append(vec_literal(emb))
                    if documents is not None:
                        sets.append("DOC=?")
                        params.append(doc)
                    if metadatas is not None:
                        sets.append("META=?")
                        params.append(json.dumps(meta or {}, ensure_ascii=False))
                    if sets:
                        c.execute(f"UPDATE {ROWS_T} SET {', '.join(sets)} WHERE COLL=? AND ID=?",
                                  [*params, self.name, rid])
                    continue
                if mode == "update":
                    continue
                if emb is None:
                    raise ValueError(f"inserting row {rid!r} requires an embedding")
                inserts.append((self.name, rid, vec_literal(emb), doc,
                                json.dumps(meta, ensure_ascii=False) if meta else "{}"))
            if inserts:
                c.executemany(f"INSERT INTO {ROWS_T} (COLL, ID, EMB, DOC, META) "
                              f"VALUES (?, ?, TO_REAL_VECTOR(?), ?, ?)", inserts)

    def add(self, ids, embeddings=None, documents=None, metadatas=None, **_):
        self._write("add", ids, embeddings, documents, metadatas)

    def upsert(self, ids, embeddings=None, documents=None, metadatas=None, **_):
        self._write("upsert", ids, embeddings, documents, metadatas)

    def update(self, ids, embeddings=None, documents=None, metadatas=None, **_):
        self._write("update", ids, embeddings, documents, metadatas)

    def delete(self, ids=None, where=None, where_document=None) -> list:
        if isinstance(ids, str):
            ids = [ids]
        if where or where_document:
            ids = self.get(ids=ids, where=where, where_document=where_document, include=[])["ids"]
        if not ids:
            return []
        ids = list(ids)
        with _conn() as c:
            for i in range(0, len(ids), _CHUNK):
                part = ids[i:i + _CHUNK]
                c.execute(f"DELETE FROM {ROWS_T} WHERE COLL=? AND ID IN ({', '.join('?' for _ in part)})",
                          [self.name, *part])
        return ids


class HanaVectorClient:
    def __init__(self):
        self.bootstrap()

    def bootstrap(self) -> None:
        with _conn() as c:
            sqldb.create_table(c, COLL_T, "",
                               f"CREATE COLUMN TABLE {COLL_T} (NAME NVARCHAR(256) PRIMARY KEY, META NCLOB, "
                               f"CREATED_AT NVARCHAR(32))")
            sqldb.create_table(c, ROWS_T, "",
                               f"CREATE COLUMN TABLE {ROWS_T} (COLL NVARCHAR(256) NOT NULL, ID NVARCHAR(512) NOT NULL, "
                               f"SEQ BIGINT GENERATED BY DEFAULT AS IDENTITY, EMB REAL_VECTOR, DOC NCLOB, META NCLOB, "
                               f"PRIMARY KEY (COLL, ID))")

    def _ensure_row(self, c, name, metadata):
        r = c.execute(f"SELECT 1 FROM {COLL_T} WHERE NAME=?", (name,)).fetchone()
        if r is None:
            sqldb.insert_new(c, COLL_T, {"NAME": name,
                                         "META": json.dumps(metadata or {"hnsw:space": "cosine"}),
                                         "CREATED_AT": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

    def get_or_create_collection(self, name, metadata=None, **_):
        with _conn() as c:
            self._ensure_row(c, name, metadata)
        return HanaCollection(self, name, metadata)

    def get_collection(self, name, **_):
        if not self.has_collection(name):
            raise ValueError(f"Collection {name} does not exist.")
        return HanaCollection(self, name)

    def has_collection(self, name) -> bool:
        with _conn() as c:
            return c.execute(f"SELECT 1 FROM {COLL_T} WHERE NAME=?", (name,)).fetchone() is not None

    def list_collections(self):
        with _conn() as c:
            rows = c.execute(f"SELECT NAME, META FROM {COLL_T} ORDER BY NAME").fetchall()
        return [HanaCollection(self, r[0], _loads(r[1])) for r in rows]

    def delete_collection(self, name):
        with _conn() as c:
            cur = c.execute(f"DELETE FROM {COLL_T} WHERE NAME=?", (name,))
            if not cur.rowcount or cur.rowcount <= 0:
                raise ValueError(f"Collection {name} does not exist.")
            c.execute(f"DELETE FROM {ROWS_T} WHERE COLL=?", (name,))

