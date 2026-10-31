"""Relational store (spec §1.5): one connection surface for KH's own tables.

SQL in callers is dialect-neutral (`?` params, bare table names). Usage:

    with sqldb.connect() as c:          # commits on success, rolls back on error, then closes/returns
        c.execute("SELECT ...", (a,)).fetchall()

Rows support `row["col"]` and `row[0]` on both backends.
"""
from __future__ import annotations

import logging
import os
import socket
import sqlite3
import threading
from typing import Any, Callable, Iterable, Sequence

from app import config

log = logging.getLogger(__name__)


def backend() -> str:
    return config.STORE_BACKEND


def is_hana() -> bool:
    return config.STORE_BACKEND == "hana"


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


class _SqliteConn(sqlite3.Connection):
    """sqlite3 connection whose context manager also closes the connection."""

    dialect = "sqlite"

    def __exit__(self, exc_type, exc, tb):  # type: ignore[override]
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def sqlite_connect(path: str | None = None) -> _SqliteConn:
    """Always-SQLite connection to the hooks DB (used by the SQLite vector / graph adapters)."""
    path = path or config.HOOKS_DB_PATH
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    c = sqlite3.connect(path, timeout=config.SQLITE_TIMEOUT_S, factory=_SqliteConn,
                        check_same_thread=False)
    c.row_factory = sqlite3.Row
    if config.SQLITE_WAL:
        try:
            c.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
    return c  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# HANA wrapper with the sqlite3 subset
# ---------------------------------------------------------------------------


class _Row:
    __slots__ = ("_names", "_vals")

    def __init__(self, names: list[str], vals: Sequence[Any]):
        self._names = names
        self._vals = list(vals)

    def __getitem__(self, k):
        if isinstance(k, (int, slice)):
            return self._vals[k]
        return self._vals[self._names.index(str(k).lower())]

    def keys(self):
        return list(self._names)

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def get(self, k, default=None):
        try:
            return self[k]
        except (ValueError, IndexError):
            return default


class _Cursor:
    def __init__(self, rows: list, rowcount: int):
        self._rows = rows
        self._i = 0
        self.rowcount = rowcount

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        r = self._rows[self._i]
        self._i += 1
        return r

    def fetchmany(self, n: int = 1):
        out = self._rows[self._i:self._i + n]
        self._i += len(out)
        return out

    def fetchall(self):
        out = self._rows[self._i:]
        self._i = len(self._rows)
        return out

    def __iter__(self):
        return iter(self.fetchall())


def _conv(v):
    if v is None or isinstance(v, (str, int, float, bytes)):
        return v
    read = getattr(v, "read", None)  # NCLOB / LOB
    if callable(read):
        try:
            data = read()
            return data.decode("utf-8") if isinstance(data, bytes) else data
        except Exception:
            return str(v)
    try:
        import decimal

        if isinstance(v, decimal.Decimal):
            return int(v) if v == int(v) else float(v)
    except Exception:
        pass
    return v


class _HanaConn:
    dialect = "hana"

    def __init__(self, pool):
        self._pool = pool
        self._conn = pool.acquire()
        self.raw = self._conn

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> _Cursor:
        cur = self._conn.cursor()
        try:
            if params:
                cur.execute(sql, tuple(params))
            else:
                cur.execute(sql)
            rows: list = []
            if cur.description:
                names = [d[0].lower() for d in cur.description]
                rows = [_Row(names, [_conv(v) for v in r]) for r in cur.fetchall()]
            return _Cursor(rows, cur.rowcount if cur.rowcount is not None else -1)
        finally:
            cur.close()

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> _Cursor:
        cur = self._conn.cursor()
        try:
            cur.executemany(sql, [tuple(p) for p in seq])
            return _Cursor([], cur.rowcount if cur.rowcount is not None else -1)
        finally:
            cur.close()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._conn is not None:
            self._pool.release(self._conn)
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
        finally:
            self.close()
        return False


def connect():
    """Open a connection on the configured relational backend."""
    if is_hana():
        from app.hana_conn import store_pool

        c = _HanaConn(store_pool())
    else:
        c = sqlite_connect()
    _ensure_core_once(c)
    return c


def dialect(c) -> str:
    return getattr(c, "dialect", "sqlite")


# ---------------------------------------------------------------------------
# DDL helpers
# ---------------------------------------------------------------------------


def table_exists(c, name: str) -> bool:
    if dialect(c) == "hana":
        r = c.execute("SELECT COUNT(*) FROM SYS.TABLES WHERE SCHEMA_NAME=CURRENT_SCHEMA AND TABLE_NAME=?",
                      (name.upper() if name.isupper() else name,)).fetchone()
        if r and r[0]:
            return True
        r = c.execute("SELECT COUNT(*) FROM SYS.TABLES WHERE SCHEMA_NAME=CURRENT_SCHEMA AND UPPER(TABLE_NAME)=?",
                      (name.upper(),)).fetchone()
        return bool(r and r[0])
    r = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return r is not None


def create_table(c, name: str, sqlite_ddl: str, hana_ddl: str) -> None:
    """SQLite DDL carries IF NOT EXISTS; HANA checks SYS.TABLES then runs CREATE COLUMN TABLE."""
    if dialect(c) == "hana":
        if not table_exists(c, name):
            c.execute(hana_ddl)
    else:
        c.execute(sqlite_ddl)


def create_index(c, name: str, table: str, cols: Sequence[str] | str, unique: bool = False) -> None:
    if isinstance(cols, str):
        cols = [cols]
    u = "UNIQUE " if unique else ""
    collist = ", ".join(cols)
    if dialect(c) == "hana":
        r = c.execute("SELECT COUNT(*) FROM SYS.INDEXES WHERE SCHEMA_NAME=CURRENT_SCHEMA AND UPPER(INDEX_NAME)=?",
                      (name.upper(),)).fetchone()
        if not (r and r[0]):
            c.execute(f"CREATE {u}INDEX {name} ON {table} ({collist})")
    else:
        c.execute(f"CREATE {u}INDEX IF NOT EXISTS {name} ON {table} ({collist})")


def column_exists(c, table: str, column: str) -> bool:
    if dialect(c) == "hana":
        r = c.execute(
            "SELECT COUNT(*) FROM SYS.TABLE_COLUMNS WHERE SCHEMA_NAME=CURRENT_SCHEMA "
            "AND UPPER(TABLE_NAME)=? AND UPPER(COLUMN_NAME)=?",
            (table.upper(), column.upper()),
        ).fetchone()
        return bool(r and r[0])
    rows = c.execute(f"PRAGMA table_info({table})").fetchall()
    return any(str(r[1]).lower() == column.lower() for r in rows)


def add_column(c, table: str, column: str, sqlite_type: str, hana_type: str | None = None) -> bool:
    """Add a column if missing; returns True when added."""
    if column_exists(c, table, column):
        return False
    if dialect(c) == "hana":
        c.execute(f"ALTER TABLE {table} ADD ({column} {hana_type or sqlite_type})")
    else:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sqlite_type}")
    return True


def is_duplicate(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        m = str(exc).lower()
        return "unique" in m or "primary key" in m
    code = getattr(exc, "errorcode", None)
    if code is None and getattr(exc, "args", None):
        a0 = exc.args[0]
        if isinstance(a0, int):
            code = a0
    return code == 301


def insert_new(c, table: str, row: dict) -> bool:
    """Insert; returns False on duplicate key."""
    cols = list(row)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    try:
        # A constraint failure aborts only the statement (SQLite ABORT / HANA), not the transaction.
        c.execute(sql, [row[k] for k in cols])
        return True
    except Exception as e:  # noqa: BLE001
        if is_duplicate(e):
            return False
        raise


def upsert(c, table: str, keys: Sequence[str] | str, row: dict) -> None:
    if isinstance(keys, str):
        keys = [keys]
    cols = list(row)
    others = [k for k in cols if k not in keys]
    if dialect(c) == "hana":
        if others:
            sets = ", ".join(f"{k}=?" for k in others)
            where = " AND ".join(f"{k}=?" for k in keys)
            cur = c.execute(f"UPDATE {table} SET {sets} WHERE {where}",
                            [row[k] for k in others] + [row[k] for k in keys])
            if cur.rowcount and cur.rowcount > 0:
                return
        if not insert_new(c, table, row) and others:
            sets = ", ".join(f"{k}=?" for k in others)
            where = " AND ".join(f"{k}=?" for k in keys)
            c.execute(f"UPDATE {table} SET {sets} WHERE {where}",
                      [row[k] for k in others] + [row[k] for k in keys])
        return
    ph = ", ".join("?" for _ in cols)
    if others:
        sets = ", ".join(f"{k}=excluded.{k}" for k in others)
        tail = f"ON CONFLICT({', '.join(keys)}) DO UPDATE SET {sets}"
    else:
        tail = f"ON CONFLICT({', '.join(keys)}) DO NOTHING"
    c.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph}) {tail}", [row[k] for k in cols])


# ---------------------------------------------------------------------------
# Database clock
# ---------------------------------------------------------------------------


def now_ms_sql(c=None) -> str:
    if (dialect(c) if c is not None else ("hana" if is_hana() else "sqlite")) == "hana":
        return "CAST(NANO100_BETWEEN(TO_TIMESTAMP('1970-01-01'), CURRENT_UTCTIMESTAMP)/10000 AS BIGINT)"
    return "CAST((julianday('now')-2440587.5)*86400000 AS INTEGER)"


def now_ms(c) -> int:
    tail = " FROM DUMMY" if dialect(c) == "hana" else ""
    return int(c.execute(f"SELECT {now_ms_sql(c)}{tail}").fetchone()[0])


# ---------------------------------------------------------------------------
# Core tables
# ---------------------------------------------------------------------------

_core_done: set[str] = set()
_core_lock = threading.Lock()


def _location(c) -> str:
    if dialect(c) == "hana":
        return f"hana:{config.HANA_HOST}:{config.HANA_SCHEMA}"
    return f"sqlite:{config.HOOKS_DB_PATH}"


def _ensure_core_once(c) -> None:
    loc = _location(c)
    if loc in _core_done:
        return
    with _core_lock:
        if loc in _core_done:
            return
        ensure_core(c)
        c.commit()
        _core_done.add(loc)


def ensure_core(c) -> None:
    create_table(
        c, "kh_schema_migration",
        "CREATE TABLE IF NOT EXISTS kh_schema_migration (module TEXT NOT NULL, version INTEGER NOT NULL, "
        "applied_ms INTEGER, PRIMARY KEY (module, version))",
        "CREATE COLUMN TABLE kh_schema_migration (module NVARCHAR(64) NOT NULL, version BIGINT NOT NULL, "
        "applied_ms BIGINT, PRIMARY KEY (module, version))",
    )
    create_table(
        c, "kh_counter",
        "CREATE TABLE IF NOT EXISTS kh_counter (name TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0)",
        "CREATE COLUMN TABLE kh_counter (name NVARCHAR(512) PRIMARY KEY, n BIGINT NOT NULL)",
    )
    create_table(
        c, "kh_lease",
        "CREATE TABLE IF NOT EXISTS kh_lease (ns TEXT NOT NULL, name TEXT NOT NULL, holder TEXT, "
        "epoch INTEGER NOT NULL DEFAULT 0, until_ms INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (ns, name))",
        "CREATE COLUMN TABLE kh_lease (ns NVARCHAR(64) NOT NULL, name NVARCHAR(256) NOT NULL, "
        "holder NVARCHAR(256) NULL, epoch BIGINT NOT NULL, until_ms BIGINT NOT NULL, PRIMARY KEY (ns, name))",
    )


def reset_core_cache() -> None:
    """Tests: forget which locations already have the core tables."""
    _core_done.clear()


# ---------------------------------------------------------------------------
# Migrations and counters
# ---------------------------------------------------------------------------


def migrate(c, module: str, steps: Sequence[tuple[int, Callable[[Any], None]]]) -> list[int]:
    """Run steps not yet in the ledger, in version order. Returns versions applied now.

    Steps must be idempotent (HANA DDL autocommits; a losing racer re-runs them harmlessly).
    """
    done = {int(r[0]) for r in c.execute(
        "SELECT version FROM kh_schema_migration WHERE module=?", (module,)).fetchall()}
    applied = []
    for version, fn in sorted(steps, key=lambda s: s[0]):
        if int(version) in done:
            continue
        fn(c)
        if insert_new(c, "kh_schema_migration",
                      {"module": module, "version": int(version), "applied_ms": now_ms(c)}):
            applied.append(int(version))
    return applied


def counter_add(c, name: str, delta: int = 1) -> int:
    """Atomic counter in the caller's transaction; returns the new value."""
    cur = c.execute("UPDATE kh_counter SET n=n+? WHERE name=?", (int(delta), name))
    if not cur.rowcount or cur.rowcount <= 0:
        if not insert_new(c, "kh_counter", {"name": name, "n": int(delta)}):
            c.execute("UPDATE kh_counter SET n=n+? WHERE name=?", (int(delta), name))
    return int(c.execute("SELECT n FROM kh_counter WHERE name=?", (name,)).fetchone()[0])


def counter_get(c, name: str) -> int:
    r = c.execute("SELECT n FROM kh_counter WHERE name=?", (name,)).fetchone()
    return int(r[0]) if r else 0


# ---------------------------------------------------------------------------
# Leases with fencing epochs
# ---------------------------------------------------------------------------


def holder_id() -> str:
    return config.CF_INSTANCE_GUID or f"{socket.gethostname()}:{os.getpid()}"


def lease_acquire(c, ns: str, name: str, holder: str, ttl_ms: int) -> dict | None:
    """Acquire or renew a lease. Returns {epoch, until_ms} when `holder` holds it, else None."""
    now = now_ms_sql(c)
    cur = c.execute(
        f"UPDATE kh_lease SET holder=?, until_ms={now}+?, "
        f"epoch = CASE WHEN holder=? THEN epoch ELSE epoch+1 END "
        f"WHERE ns=? AND name=? AND (until_ms < {now} OR holder=?)",
        (holder, int(ttl_ms), holder, ns, name, holder),
    )
    if not cur.rowcount or cur.rowcount <= 0:
        exists = c.execute("SELECT 1 FROM kh_lease WHERE ns=? AND name=?", (ns, name)).fetchone()
        if exists:
            return None
        until = now_ms(c) + int(ttl_ms)
        if not insert_new(c, "kh_lease", {"ns": ns, "name": name, "holder": holder, "epoch": 1,
                                          "until_ms": until}):
            return None
    r = c.execute("SELECT holder, epoch, until_ms FROM kh_lease WHERE ns=? AND name=?", (ns, name)).fetchone()
    if r is None or r[0] != holder:
        return None
    return {"epoch": int(r[1]), "until_ms": int(r[2])}


def lease_renew(c, ns: str, name: str, holder: str, epoch: int, ttl_ms: int) -> bool:
    now = now_ms_sql(c)
    cur = c.execute(
        f"UPDATE kh_lease SET until_ms={now}+? WHERE ns=? AND name=? AND holder=? AND epoch=? "
        f"AND until_ms >= {now}",
        (int(ttl_ms), ns, name, holder, int(epoch)),
    )
    return bool(cur.rowcount and cur.rowcount > 0)


def lease_release(c, ns: str, name: str, holder: str, epoch: int | None = None) -> bool:
    """Sets until_ms=0 (row kept, epoch kept)."""
    if epoch is None:
        cur = c.execute("UPDATE kh_lease SET until_ms=0 WHERE ns=? AND name=? AND holder=?", (ns, name, holder))
    else:
        cur = c.execute("UPDATE kh_lease SET until_ms=0 WHERE ns=? AND name=? AND holder=? AND epoch=?",
                        (ns, name, holder, int(epoch)))
    return bool(cur.rowcount and cur.rowcount > 0)


def lease_bump_all(c, ns: str) -> int:
    """Epoch+1 and holder NULL for every lease of a namespace (fences out all current holders)."""
    cur = c.execute("UPDATE kh_lease SET epoch=epoch+1, holder=NULL WHERE ns=?", (ns,))
    return int(cur.rowcount or 0)


def lease_get(c, ns: str, name: str) -> dict | None:
    r = c.execute("SELECT holder, epoch, until_ms FROM kh_lease WHERE ns=? AND name=?", (ns, name)).fetchone()
    if r is None:
        return None
    return {"holder": r[0], "epoch": int(r[1]), "until_ms": int(r[2])}


def fence_sql(c=None) -> str:
    """SQL predicate to AND into fenced writes; params (ns, name, epoch)."""
    now = now_ms_sql(c)
    return (f"EXISTS (SELECT 1 FROM kh_lease WHERE ns=? AND name=? AND epoch=? "
            f"AND holder IS NOT NULL AND until_ms >= {now})")


def run_under_lease(name: str, ttl: float, fn: Callable[[], Any], ns: str = "_kh") -> Any:
    """Run `fn` only if this instance holds lease (ns, name). The lease is never released (sticky leader).

    `ttl` is in seconds (crons pass interval*1.5). Returns fn()'s result, or None when skipped.
    """
    with connect() as c:
        got = lease_acquire(c, ns, name, holder_id(), int(float(ttl) * 1000))
    if got is None:
        log.debug("lease %s/%s held elsewhere; skip", ns, name)
        return None
    return fn()
