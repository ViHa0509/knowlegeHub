"""Event store (spec §1.15, §4.9): `SqlEventStore` on table `kh_event`.

Gapless per-namespace `seq` (counter row `ev:<ns>` serialises appends); ids already stored (or repeated inside
one batch) keep their first seq and are reported `duplicate: true`. Cursor reads `seq > after_seq ORDER BY seq`.
The same table backs the stage-F `/v1` event store (§6.4).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Callable, Iterable, Sequence

from app import sqldb

log = logging.getLogger(__name__)

COLUMNS = ("ns", "seq", "id", "project", "kind", "ts_ms", "received_ms", "run_id", "unit_id", "session_id",
           "attempt", "epoch", "actor_json", "schema_ver", "ix_s1", "ix_s2", "ix_s3", "ix_s4", "ix_n1", "ix_n2",
           "body_json")
GROUP_FIELDS = ("kind", "project", "session_id", "ix_s1", "ix_s2")

_SQLITE = (
    "CREATE TABLE IF NOT EXISTS kh_event (ns TEXT NOT NULL, seq INTEGER NOT NULL, id TEXT NOT NULL, project TEXT, "
    "kind TEXT NOT NULL, ts_ms INTEGER, received_ms INTEGER, run_id TEXT, unit_id TEXT, session_id TEXT, "
    "attempt INTEGER, epoch INTEGER, actor_json TEXT, schema_ver INTEGER, ix_s1 TEXT, ix_s2 TEXT, ix_s3 TEXT, "
    "ix_s4 TEXT, ix_n1 REAL, ix_n2 REAL, body_json TEXT, PRIMARY KEY (ns, seq))"
)
_HANA = (
    "CREATE COLUMN TABLE kh_event (ns NVARCHAR(64) NOT NULL, seq BIGINT NOT NULL, id NVARCHAR(64) NOT NULL, "
    "project NVARCHAR(256), kind NVARCHAR(128) NOT NULL, ts_ms BIGINT, received_ms BIGINT, run_id NVARCHAR(128), "
    "unit_id NVARCHAR(256), session_id NVARCHAR(128), attempt INTEGER, epoch BIGINT, actor_json NCLOB, "
    "schema_ver INTEGER, ix_s1 NVARCHAR(512), ix_s2 NVARCHAR(512), ix_s3 NVARCHAR(512), ix_s4 NVARCHAR(512), "
    "ix_n1 DOUBLE, ix_n2 DOUBLE, body_json NCLOB, PRIMARY KEY (ns, seq))"
)


def ensure(c) -> None:
    sqldb.create_table(c, "kh_event", _SQLITE, _HANA)
    sqldb.create_index(c, "kh_event_id", "kh_event", ["ns", "id"], unique=True)
    sqldb.create_index(c, "kh_event_kind", "kh_event", ["ns", "kind", "seq"])
    sqldb.create_index(c, "kh_event_session", "kh_event", ["ns", "session_id", "seq"])


def decode(row: Any) -> dict:
    """A stored row -> API dict (`body`, `actor` decoded)."""
    d = {k: row[k] for k in COLUMNS}
    for src, dst in (("body_json", "body"), ("actor_json", "actor")):
        raw = d.pop(src, None)
        try:
            d[dst] = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            d[dst] = {"raw": raw}
    return d


class SqlEventStore:
    """EventStore port on the relational store."""

    def __init__(self):
        self._ready = False
        self._lock = threading.Lock()

    def _init(self, c) -> None:
        if self._ready:
            return
        with self._lock:
            if not self._ready:
                ensure(c)
                self._ready = True

    def init(self) -> None:
        with sqldb.connect() as c:
            self._init(c)

    # -- writes -------------------------------------------------------------------------------------------
    def append(self, ns: str, rows: Sequence[dict], guard: Callable[[Any], bool] | None = None) -> list[dict]:
        """Append rows in one transaction. Returns [{id, seq, duplicate}] in input order."""
        last: Exception | None = None
        for _attempt in range(3):
            try:
                return self._append_once(ns, rows, guard)
            except Exception as e:  # noqa: BLE001
                if sqldb.is_duplicate(e):
                    last = e
                    continue
                raise
        raise last  # type: ignore[misc]

    def _append_once(self, ns, rows, guard):
        if not rows:
            return []
        with sqldb.connect() as c:
            self._init(c)
            if guard is not None and not guard(c):
                raise PermissionError("fence lost")
            now = sqldb.now_ms(c)
            ids = [str(r.get("id") or "") for r in rows]
            known: dict[str, int] = {}
            uniq = sorted({i for i in ids if i})
            for k in range(0, len(uniq), 200):
                part = uniq[k:k + 200]
                q = f"SELECT id, seq FROM kh_event WHERE ns=? AND id IN ({', '.join('?' for _ in part)})"
                for r in c.execute(q, [ns, *part]).fetchall():
                    known[str(r[0])] = int(r[1])
            fresh = []
            seen_batch: set[str] = set()
            for i, r in zip(ids, rows):
                if not i or i in known or i in seen_batch:
                    continue
                seen_batch.add(i)
                fresh.append(r)
            if fresh:
                top = sqldb.counter_add(c, f"ev:{ns}", len(fresh))
                seq = top - len(fresh)
                cols = ", ".join(COLUMNS)
                ph = ", ".join("?" for _ in COLUMNS)
                for r in fresh:
                    seq += 1
                    vals = dict(r)
                    vals["ns"] = ns
                    vals["seq"] = seq
                    vals["received_ms"] = now
                    if not vals.get("ts_ms"):
                        vals["ts_ms"] = now
                    for j in ("actor_json", "body_json"):
                        if isinstance(vals.get(j), (dict, list)):
                            vals[j] = json.dumps(vals[j], ensure_ascii=False, default=str)
                    c.execute(f"INSERT INTO kh_event ({cols}) VALUES ({ph})", [vals.get(k) for k in COLUMNS])
                    known[str(r["id"])] = seq
            out, first = [], set()
            fresh_ids = {str(r["id"]) for r in fresh}
            for i in ids:
                dup = not (i in fresh_ids and i not in first)
                first.add(i)
                out.append({"id": i, "seq": known.get(i), "duplicate": dup})
            return out

    # -- reads --------------------------------------------------------------------------------------------
    def read(self, ns: str, after_seq: int = 0, kinds: Iterable[str] | None = None,
             hidden: Iterable[str] | None = None, filters: dict | None = None, limit: int = 100) -> list[dict]:
        where, params = ["ns=?", "seq>?"], [ns, int(after_seq or 0)]
        kinds = [k for k in (kinds or []) if k]
        if kinds:
            where.append(f"kind IN ({', '.join('?' for _ in kinds)})")
            params += kinds
        hidden = [k for k in (hidden or []) if k]
        if hidden:
            where.append(f"kind NOT IN ({', '.join('?' for _ in hidden)})")
            params += hidden
        for col, val in (filters or {}).items():
            if col not in COLUMNS or val is None:
                continue
            if col == "ts_ms_min":
                continue
            where.append(f"{col}=?")
            params.append(val)
        with sqldb.connect() as c:
            self._init(c)
            q = (f"SELECT {', '.join(COLUMNS)} FROM kh_event WHERE {' AND '.join(where)} "
                 f"ORDER BY seq LIMIT {max(1, int(limit))}")
            return [decode(r) for r in c.execute(q, params).fetchall()]

    def get(self, ns: str, event_id: str) -> dict | None:
        with sqldb.connect() as c:
            self._init(c)
            r = c.execute(f"SELECT {', '.join(COLUMNS)} FROM kh_event WHERE ns=? AND id=?", (ns, event_id)).fetchone()
            return decode(r) if r else None

    def stats(self, ns: str, group_by: Sequence[str] = ("kind",), since_ms: int | None = None,
              hidden: Iterable[str] | None = None) -> list[dict]:
        cols = [g for g in group_by if g in GROUP_FIELDS] or ["kind"]
        where, params = ["ns=?"], [ns]
        if since_ms:
            where.append("ts_ms>=?")
            params.append(int(since_ms))
        hidden = [k for k in (hidden or []) if k]
        if hidden:
            where.append(f"kind NOT IN ({', '.join('?' for _ in hidden)})")
            params += hidden
        sel = ", ".join(cols)
        with sqldb.connect() as c:
            self._init(c)
            q = (f"SELECT {sel}, COUNT(*) AS n FROM kh_event WHERE {' AND '.join(where)} GROUP BY {sel} "
                 f"ORDER BY n DESC")
            out = []
            for r in c.execute(q, params).fetchall():
                d = {k: r[i] for i, k in enumerate(cols)}
                d["n"] = int(r[len(cols)])
                out.append(d)
            return out

    def count(self, ns: str) -> int:
        with sqldb.connect() as c:
            self._init(c)
            return int(c.execute("SELECT COUNT(*) FROM kh_event WHERE ns=?", (ns,)).fetchone()[0])


_store: SqlEventStore | None = None


def get_store() -> SqlEventStore:
    """The configured store (`KH_EVENT_STORE`, only `sql` exists)."""
    global _store
    if _store is None:
        kind = (os.environ.get("KH_EVENT_STORE") or "sql").strip().lower()
        if kind != "sql":
            log.warning("KH_EVENT_STORE=%s unknown; using sql", kind)
        _store = SqlEventStore()
    return _store
