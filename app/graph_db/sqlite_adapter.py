"""SQLite graph adapter (rebuild contract): the HANA tables (§1.12) as plain SQL in the hooks DB, no OpenCypher.

Tables per prefix P: <P>DOCUMENTS, <P>GRAPH_NODES, <P>GRAPH_EDGES.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Iterable, Sequence

from app import sqldb

log = logging.getLogger(__name__)
_CHUNK = 500


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _attrs(v) -> str:
    if v is None:
        return "{}"
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def _parse(v):
    if not v:
        return {}
    try:
        return json.loads(v)
    except (ValueError, TypeError):
        return v


class SqliteGraphAdapter:
    backend = "sqlite"

    def __init__(self, prefix: str = "KH_", path: str | None = None):
        self.prefix = prefix
        self.path = path
        self.T_DOC = f"{prefix}DOCUMENTS"
        self.T_NODE = f"{prefix}GRAPH_NODES"
        self.T_EDGE = f"{prefix}GRAPH_EDGES"

    def _c(self):
        return sqldb.sqlite_connect(self.path)

    def probe(self) -> bool:
        with self._c() as c:
            c.execute("SELECT 1").fetchone()
        return True

    def bootstrap(self) -> None:
        with self._c() as c:
            c.execute(f"CREATE TABLE IF NOT EXISTS {self.T_DOC} (ID INTEGER PRIMARY KEY AUTOINCREMENT, "
                      f"CHECKSUM TEXT UNIQUE, SOURCE_TEXT TEXT, CREATED_AT TEXT)")
            c.execute(f"CREATE TABLE IF NOT EXISTS {self.T_NODE} (CORE_ID TEXT PRIMARY KEY, ENTITY_TYPE TEXT, "
                      f"LABEL TEXT, ATTRIBUTES TEXT, DOCUMENT_ID INTEGER, SOURCE_TEXT TEXT, CREATED_AT TEXT, "
                      f"UPDATED_AT TEXT)")
            c.execute(f"CREATE TABLE IF NOT EXISTS {self.T_EDGE} (ID INTEGER PRIMARY KEY AUTOINCREMENT, "
                      f"SOURCE_CORE_ID TEXT, RELATION TEXT, TARGET_CORE_ID TEXT, CREATED_AT TEXT, ATTRIBUTES TEXT, "
                      f"SOURCE_TEXT TEXT, CONFIDENCE REAL DEFAULT 1.0, "
                      f"UNIQUE (SOURCE_CORE_ID, RELATION, TARGET_CORE_ID))")
            for col, typ in (("ATTRIBUTES", "TEXT"), ("SOURCE_TEXT", "TEXT"), ("CONFIDENCE", "REAL DEFAULT 1.0")):
                sqldb.add_column(c, self.T_EDGE, col, typ)

    def drop(self) -> bool:
        with self._c() as c:
            for t in (self.T_EDGE, self.T_NODE, self.T_DOC):
                c.execute(f"DROP TABLE IF EXISTS {t}")
        return True

    # -- writes -----------------------------------------------------------
    def _document_id(self, c, text: str | None):
        if not text:
            return None
        chk = hashlib.sha256(text.encode("utf-8")).hexdigest()
        r = c.execute(f"SELECT ID FROM {self.T_DOC} WHERE CHECKSUM=?", (chk,)).fetchone()
        if r:
            return int(r[0])
        c.execute(f"INSERT OR IGNORE INTO {self.T_DOC} (CHECKSUM, SOURCE_TEXT, CREATED_AT) VALUES (?, ?, ?)",
                  (chk, text, _now()))
        return int(c.execute(f"SELECT ID FROM {self.T_DOC} WHERE CHECKSUM=?", (chk,)).fetchone()[0])

    def upsert_nodes(self, nodes: Iterable[dict]) -> int:
        n = 0
        with self._c() as c:
            for nd in nodes:
                st = nd.get("source_text") or ""
                doc_id = self._document_id(c, st)
                now = _now()
                c.execute(
                    f"INSERT INTO {self.T_NODE} (CORE_ID, ENTITY_TYPE, LABEL, ATTRIBUTES, DOCUMENT_ID, SOURCE_TEXT, "
                    f"CREATED_AT, UPDATED_AT) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(CORE_ID) DO UPDATE SET "
                    f"ENTITY_TYPE=excluded.ENTITY_TYPE, LABEL=excluded.LABEL, ATTRIBUTES=excluded.ATTRIBUTES, "
                    f"DOCUMENT_ID=excluded.DOCUMENT_ID, SOURCE_TEXT=excluded.SOURCE_TEXT, UPDATED_AT=excluded.UPDATED_AT",
                    (nd["core_id"], nd.get("type") or nd.get("entity_type"), nd.get("label"),
                     _attrs(nd.get("attributes")), doc_id, st, now, now))
                n += 1
        return n

    def upsert_edges(self, edges: Iterable[dict]) -> int:
        n = 0
        with self._c() as c:
            for e in edges:
                conf = e.get("confidence")
                c.execute(
                    f"INSERT INTO {self.T_EDGE} (SOURCE_CORE_ID, RELATION, TARGET_CORE_ID, CREATED_AT, ATTRIBUTES, "
                    f"SOURCE_TEXT, CONFIDENCE) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    f"ON CONFLICT(SOURCE_CORE_ID, RELATION, TARGET_CORE_ID) DO UPDATE SET "
                    f"ATTRIBUTES=excluded.ATTRIBUTES, SOURCE_TEXT=excluded.SOURCE_TEXT, CONFIDENCE=excluded.CONFIDENCE",
                    (e["source_core_id"], e["relation"], e["target_core_id"], _now(), _attrs(e.get("attributes")),
                     e.get("source_text") or "", 1.0 if conf is None else float(conf)))
                n += 1
        return n

    # -- reads ------------------------------------------------------------
    @staticmethod
    def _in(ids: Sequence[str]) -> str:
        return ", ".join("?" for _ in ids)

    def _node_dicts(self, c, ids: Sequence[str]) -> list[dict]:
        out = []
        ids = list(ids)
        for i in range(0, len(ids), _CHUNK):
            part = ids[i:i + _CHUNK]
            for r in c.execute(f"SELECT CORE_ID, ENTITY_TYPE, LABEL, ATTRIBUTES, SOURCE_TEXT, CREATED_AT, UPDATED_AT "
                               f"FROM {self.T_NODE} WHERE CORE_ID IN ({self._in(part)})", part).fetchall():
                out.append({"core_id": r[0], "entity_type": r[1], "label": r[2], "attributes": _parse(r[3]),
                            "source_text": r[4], "created_at": r[5], "updated_at": r[6]})
        return out

    def fetch_graph(self, core_ids: Sequence[str], hierarchy_edges: Sequence[str], hops: int = 3):
        try:
            with self._c() as c:
                expanded = set(core_ids)
                frontier = set(core_ids)
                rels = list(hierarchy_edges)
                for _ in range(max(0, int(hops))):
                    if not frontier:
                        break
                    fl = list(frontier)
                    rows = c.execute(
                        f"SELECT SOURCE_CORE_ID FROM {self.T_EDGE} WHERE TARGET_CORE_ID IN ({self._in(fl)}) "
                        f"AND RELATION IN ({self._in(rels)})", [*fl, *rels]).fetchall()
                    new = {r[0] for r in rows} - expanded
                    expanded |= new
                    frontier = new
                el = list(expanded)
                edges, seen = [], set()
                for i in range(0, len(el), _CHUNK):
                    part = el[i:i + _CHUNK]
                    for r in c.execute(
                            f"SELECT SOURCE_CORE_ID, RELATION, TARGET_CORE_ID, ATTRIBUTES, SOURCE_TEXT, CONFIDENCE, "
                            f"CREATED_AT FROM {self.T_EDGE} WHERE SOURCE_CORE_ID IN ({self._in(part)}) "
                            f"OR TARGET_CORE_ID IN ({self._in(part)})", [*part, *part]).fetchall():
                        key = (r[0], r[1], r[2])
                        if key in seen:
                            continue
                        seen.add(key)
                        edges.append({"source_core_id": r[0], "relation": r[1], "target_core_id": r[2],
                                      "attributes": _parse(r[3]), "source_text": r[4],
                                      "confidence": r[5], "created_at": r[6]})
                involved = set(expanded)
                for e in edges:
                    involved.add(e["source_core_id"])
                    involved.add(e["target_core_id"])
                nodes = self._node_dicts(c, sorted(involved))
            return nodes, edges
        except Exception as e:  # noqa: BLE001
            log.warning("fetch_graph failed: %s", e)
            return [], []

    def delete_nodes(self, ids: Sequence[str]) -> dict:
        ids = list(ids)
        nodes = edges = 0
        with self._c() as c:
            for i in range(0, len(ids), _CHUNK):
                part = ids[i:i + _CHUNK]
                edges += c.execute(f"DELETE FROM {self.T_EDGE} WHERE SOURCE_CORE_ID IN ({self._in(part)}) "
                                   f"OR TARGET_CORE_ID IN ({self._in(part)})", [*part, *part]).rowcount or 0
                nodes += c.execute(f"DELETE FROM {self.T_NODE} WHERE CORE_ID IN ({self._in(part)})",
                                   part).rowcount or 0
        return {"nodes_deleted": nodes, "edges_deleted": edges}

    def delete_orphan_documents(self) -> int:
        with self._c() as c:
            cur = c.execute(f"DELETE FROM {self.T_DOC} WHERE ID NOT IN "
                            f"(SELECT DOCUMENT_ID FROM {self.T_NODE} WHERE DOCUMENT_ID IS NOT NULL)")
            return int(cur.rowcount or 0)

    def traverse_logic(self, start_ids: Sequence[str], relations: Sequence[str] | None = None,
                       max_depth: int = 3) -> list[dict]:
        """BFS over content edges from start_ids; returns edges in discovery order."""
        seen_nodes, frontier, out, seen = set(start_ids), set(start_ids), [], set()
        with self._c() as c:
            for _ in range(max(0, int(max_depth))):
                if not frontier:
                    break
                fl = list(frontier)
                sql = (f"SELECT SOURCE_CORE_ID, RELATION, TARGET_CORE_ID FROM {self.T_EDGE} "
                       f"WHERE SOURCE_CORE_ID IN ({self._in(fl)})")
                params = list(fl)
                if relations:
                    sql += f" AND RELATION IN ({self._in(relations)})"
                    params += list(relations)
                nxt = set()
                for r in c.execute(sql, params).fetchall():
                    if (r[0], r[1], r[2]) not in seen:
                        seen.add((r[0], r[1], r[2]))
                        out.append({"source_core_id": r[0], "relation": r[1], "target_core_id": r[2]})
                    if r[2] not in seen_nodes:
                        seen_nodes.add(r[2])
                        nxt.add(r[2])
                frontier = nxt
        return out
