"""lifecycle (spec §6.15): namespace export / import / purge, event retention sweep, global agents stop/resume.

Discovery is generic: every loaded module's `TABLES` + `LOCAL_TABLES` take part when the table exists and has an
`ns` column, plus core `kh_lease` (ns column) and `kh_counter` rows whose name contains `:<ns>` (written as
`:{ns}`). `kh_ns` itself travels in `manifest.json`; `kh_event_week` in `_kh/`; vault tables only with
`include_vault`. Vector collections `ns_<ns>_<suffix>` are exported as text + metadata (never embeddings) and
re-embedded on import.

Optional module hooks (looked up on every loaded module): `purge_ns(ns)` (extra cleanup such as graphs
`<ns>__*`; runs before the ns rows are deleted) and `rebuild_ns(ns)` (rebuild projections after an import).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
import secrets
import tempfile
import threading
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterator

from fastapi import FastAPI, Query, Request
from fastapi.responses import StreamingResponse

from app import config, sqldb
from app.caps import core
from app.caps.core import CapError

log = logging.getLogger(__name__)

NAME = "lifecycle"
VERSION = "1.0.0"
STATUS = "beta"
DEFAULT_ON = True
FORMAT = "kh-ns-export/1"
EXPORT_PAGE = 1000
DAY_MS = 86_400_000
TABLES = ["kh_event_week", "kh_ns_export"]
LOCAL_TABLES: list[str] = []
STOP_EXEMPT = frozenset({core.PEOPLE_NS})
OWN_TABLES = frozenset({"kh_ns", "kh_ns_export", "kh_event_week"})
RESUME_PURGE_AFTER_MS = 3_600_000
PROMPT_KIND = "kh.session.prompt"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
VEC_STAMPS = ("source_text", "emb_model", "emb_ver", "emb_dim")


def purge_batch() -> int:
    return max(1, config._int("KH_PURGE_BATCH", 5000))


def import_max_bytes() -> int:
    return config._int("KH_IMPORT_MAX_BYTES", 8 * 1024 ** 3)


LIMITS = {"purge_batch": 5000, "export_page": EXPORT_PAGE, "import_max_bytes": 8 * 1024 ** 3}


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _m1(c) -> None:
    sqldb.create_table(
        c, "kh_event_week",
        "CREATE TABLE IF NOT EXISTS kh_event_week (ns TEXT NOT NULL, project TEXT NOT NULL, kind TEXT NOT NULL, "
        "week TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (ns, project, kind, week))",
        "CREATE COLUMN TABLE kh_event_week (ns NVARCHAR(64) NOT NULL, project NVARCHAR(256) NOT NULL, "
        "kind NVARCHAR(128) NOT NULL, week NVARCHAR(10) NOT NULL, n BIGINT NOT NULL, "
        "PRIMARY KEY (ns, project, kind, week))")
    sqldb.create_table(
        c, "kh_ns_export",
        "CREATE TABLE IF NOT EXISTS kh_ns_export (ns TEXT NOT NULL, export_id TEXT NOT NULL, token_sha TEXT NOT NULL, "
        "include_vault INTEGER NOT NULL DEFAULT 0, state_at_export TEXT, actor TEXT, created_ms INTEGER, "
        "completed_ms INTEGER, checksums_json TEXT, PRIMARY KEY (ns, export_id))",
        "CREATE COLUMN TABLE kh_ns_export (ns NVARCHAR(64) NOT NULL, export_id NVARCHAR(64) NOT NULL, "
        "token_sha NVARCHAR(64) NOT NULL, include_vault INTEGER NOT NULL, state_at_export NVARCHAR(16), "
        "actor NVARCHAR(256), created_ms BIGINT, completed_ms BIGINT, checksums_json NCLOB, "
        "PRIMARY KEY (ns, export_id))")


def init(c) -> None:
    sqldb.migrate(c, NAME, [(1, _m1)])


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _loaded() -> list:
    from app import caps

    return list(caps.LOADED)


def _columns(c, table: str) -> tuple[list[str], list[str]]:
    """(columns in table order, primary-key columns in key order), lower-cased."""
    if sqldb.dialect(c) == "hana":
        cols = [str(r[0]).lower() for r in c.execute(
            "SELECT COLUMN_NAME FROM SYS.TABLE_COLUMNS WHERE SCHEMA_NAME=CURRENT_SCHEMA AND UPPER(TABLE_NAME)=? "
            "ORDER BY POSITION", (table.upper(),)).fetchall()]
        pk = [str(r[0]).lower() for r in c.execute(
            "SELECT COLUMN_NAME FROM SYS.CONSTRAINTS WHERE SCHEMA_NAME=CURRENT_SCHEMA AND UPPER(TABLE_NAME)=? "
            "AND IS_PRIMARY_KEY='TRUE' ORDER BY POSITION", (table.upper(),)).fetchall()]
        return cols, pk
    info = c.execute(f"PRAGMA table_info({table})").fetchall()
    cols = [str(r[1]).lower() for r in info]
    pk = [str(r[1]).lower() for r in sorted((r for r in info if int(r[5] or 0) > 0), key=lambda r: int(r[5]))]
    return cols, pk


def vault_tables() -> set[str]:
    out: set[str] = set()
    for mod in _loaded():
        if getattr(mod, "NAME", "") == "vault":
            out.update(getattr(mod, "TABLES", []) or [])
            out.update(getattr(mod, "LOCAL_TABLES", []) or [])
    return out


def ns_tables(c, include_vault: bool = True) -> list[tuple[str, list[str], list[str]]]:
    """[(table, columns, pk)] taking part in export/import/purge, in module order, then kh_lease."""
    seen: list[str] = []
    vt = vault_tables()
    for mod in _loaded():
        for t in list(getattr(mod, "TABLES", []) or []) + list(getattr(mod, "LOCAL_TABLES", []) or []):
            if t in OWN_TABLES or t in seen:
                continue
            if t in vt and not include_vault:
                continue
            seen.append(t)
    seen.append("kh_lease")
    out = []
    for t in seen:
        if not sqldb.table_exists(c, t):
            continue
        cols, pk = _columns(c, t)
        if "ns" not in cols:
            continue
        out.append((t, cols, pk or cols))
    return out


def _counter_re(ns: str) -> re.Pattern:
    return re.compile(rf":{re.escape(ns)}(?=$|:)")


def ns_counters(c, ns: str) -> list[tuple[str, int]]:
    rx = _counter_re(ns)
    rows = c.execute("SELECT name, n FROM kh_counter WHERE name LIKE ? ORDER BY name", (f"%:{ns}%",)).fetchall()
    return [(r[0], int(r[1])) for r in rows if rx.search(r[0])]


def vector_suffixes(ns: str) -> list[str]:
    from app import vector_store as vs

    pre = f"ns_{ns}_"
    try:
        return sorted(n[len(pre):] for n in vs.list_collection_names() if n.startswith(pre))
    except Exception as e:  # noqa: BLE001
        log.debug("vector collections not listed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Row encoding
# ---------------------------------------------------------------------------


def _norm(v: Any) -> Any:
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"$b64": base64.b64encode(bytes(v)).decode("ascii")}
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return v


def _denorm(v: Any) -> Any:
    if isinstance(v, dict) and set(v) == {"$b64"}:
        return base64.b64decode(v["$b64"])
    return v


def line(obj: Any) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def table_lines(c, table: str, cols: list[str], pk: list[str], ns: str) -> Iterator[bytes]:
    order = ", ".join(pk)
    off = 0
    while True:
        rows = c.execute(f"SELECT * FROM {table} WHERE ns=? ORDER BY {order} LIMIT {EXPORT_PAGE} OFFSET {off}",
                         (ns,)).fetchall()
        for r in rows:
            d = {str(k).lower(): _norm(r[k]) for k in r.keys()}
            d.pop("ns", None)
            yield line(d)
        if len(rows) < EXPORT_PAGE:
            return
        off += EXPORT_PAGE


def vector_lines(ns: str, suffix: str) -> Iterator[bytes]:
    from app import vector_store as vs

    coll = vs.get_existing(f"ns_{ns}_{suffix}")
    if coll is None:
        return
    rows = []
    off = 0
    while True:
        part = vs.get_vt(coll, limit=EXPORT_PAGE, offset=off)
        rows.extend(part)
        if len(part) < EXPORT_PAGE:
            break
        off += EXPORT_PAGE
    for r in sorted(rows, key=lambda x: str(x["vector_id"])):
        meta = {k: _norm(v) for k, v in (r.get("metadata") or {}).items() if k not in VEC_STAMPS}
        yield line({"id": r["vector_id"], "text": r.get("source_text"), "metadata": meta})


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _ns_row(c, ns: str) -> dict | None:
    r = c.execute("SELECT * FROM kh_ns WHERE ns=?", (ns,)).fetchone()
    return {str(k).lower(): _norm(r[k]) for k in r.keys()} if r else None


def _check_admin(row: dict | None, ns: str) -> dict:
    if row is None or (row.get("state") == "purged"):
        raise CapError("not_found", f"namespace '{ns}' does not exist")
    core.admin_person([], core.decode_ns_row(_RowView(row)))
    return row


class _RowView(dict):
    def keys(self):  # decode_ns_row expects .keys() + [] access
        return list(super().keys())


def _zinfo(name: str) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name, date_time=ZIP_DATE)
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = 0o644 << 16
    return zi


def build_export(ns: str, include_vault: bool) -> tuple[Any, str, str]:
    """-> (spooled zip file at 0, export_id, confirm token)."""
    with sqldb.connect() as c:
        row = _ns_row(c, ns)
    row = _check_admin(row, ns)
    actor = core.admin_person([], core.decode_ns_row(_RowView(row)))
    export_id = core.new_id()
    token = "kx_" + secrets.token_urlsafe(24)
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        sqldb.insert_new(c, "kh_ns_export", {"ns": ns, "export_id": export_id, "token_sha": core.sha256_text(token),
                                             "include_vault": 1 if include_vault else 0,
                                             "state_at_export": row.get("state"), "actor": actor.principal,
                                             "created_ms": now, "completed_ms": None, "checksums_json": None})
    if include_vault:
        core.emit(ns, "kh.vault_read", {"what": "export", "export_id": export_id, "actor": actor.principal})
    out = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024)
    checksums: dict[str, dict] = {}
    files: list[dict] = []

    def put(zf: zipfile.ZipFile, name: str, chunks) -> None:
        h = hashlib.sha256()
        n = 0
        with zf.open(_zinfo(name), "w") as fh:
            for b in chunks:
                h.update(b)
                n += 1
                fh.write(b)
        checksums[name] = {"sha256": h.hexdigest(), "lines": n}

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf, sqldb.connect() as c:
        tables = ns_tables(c, include_vault)
        for t, cols, pk in tables:
            put(zf, f"tables/{t}.ndjson", table_lines(c, t, cols, pk, ns))
            files.append({"path": f"tables/{t}.ndjson", "table": t, "columns": [x for x in cols if x != "ns"],
                          "pk": [x for x in pk if x != "ns"]})
        rx = _counter_re(ns)
        put(zf, "tables/kh_counter.ndjson",
            (line({"name": rx.sub(":{ns}", name), "n": n}) for name, n in ns_counters(c, ns)))
        files.append({"path": "tables/kh_counter.ndjson", "table": "kh_counter", "columns": ["name", "n"],
                      "pk": ["name"]})
        if sqldb.table_exists(c, "kh_event_week"):
            put(zf, "_kh/kh_event_week.ndjson", table_lines(c, "kh_event_week", [], ["project", "kind", "week"], ns))
            files.append({"path": "_kh/kh_event_week.ndjson", "table": "kh_event_week"})
        for suffix in vector_suffixes(ns):
            put(zf, f"vectors/{suffix}.ndjson", vector_lines(ns, suffix))
            files.append({"path": f"vectors/{suffix}.ndjson", "collection_suffix": suffix})
        migrations = {}
        for mod in _loaded():
            mname = getattr(mod, "NAME", "")
            migrations[mname] = {"version": getattr(mod, "VERSION", None), "migrations": [int(r[0]) for r in c.execute(
                "SELECT version FROM kh_schema_migration WHERE module=? ORDER BY version", (mname,)).fetchall()]}
        manifest = {"format": FORMAT, "ns": ns, "export_id": export_id, "created_ms": now,
                    "include_vault": include_vault, "confirm_token": token, "namespace": row,
                    "modules": migrations, "files": files, "kh_version": config.KH_VERSION}
        mbytes = json.dumps(manifest, sort_keys=True, indent=1, ensure_ascii=False).encode("utf-8")
        with zf.open(_zinfo("manifest.json"), "w") as fh:
            fh.write(mbytes)
        checksums["manifest.json"] = {"sha256": core.sha256_text(mbytes), "lines": mbytes.count(b"\n") + 1}
        with zf.open(_zinfo("checksums.json"), "w") as fh:
            fh.write(json.dumps(checksums, sort_keys=True, indent=1).encode("utf-8"))
    with sqldb.connect() as c:
        c.execute("UPDATE kh_ns_export SET completed_ms=?, checksums_json=? WHERE ns=? AND export_id=?",
                  (sqldb.now_ms(c), core.dumps(checksums), ns, export_id))
    out.seek(0)
    return out, export_id, token


def _iter_file(fh, chunk: int = 65536) -> Iterator[bytes]:
    try:
        while True:
            b = fh.read(chunk)
            if not b:
                return
            yield b
    finally:
        fh.close()


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def _read_lines(zf: zipfile.ZipFile, name: str) -> Iterator[dict]:
    with zf.open(name) as fh:
        for raw in io.TextIOWrapper(fh, encoding="utf-8"):
            if raw.strip():
                yield json.loads(raw)


def _verify(zf: zipfile.ZipFile) -> tuple[dict, dict]:
    names = set(zf.namelist())
    if "manifest.json" not in names or "checksums.json" not in names:
        raise CapError("invalid", "not a namespace export: manifest.json / checksums.json missing")
    manifest = json.loads(zf.read("manifest.json"))
    if manifest.get("format") != FORMAT:
        raise CapError("invalid", f"unknown export format {manifest.get('format')!r}; expected {FORMAT}")
    sums = json.loads(zf.read("checksums.json"))
    for name, s in sums.items():
        if name not in names:
            raise CapError("invalid", f"export file {name} is missing")
        h = hashlib.sha256()
        with zf.open(name) as fh:
            for b in iter(lambda: fh.read(65536), b""):
                h.update(b)
        if h.hexdigest() != s.get("sha256"):
            raise CapError("invalid", f"checksum mismatch for {name}")
    return manifest, sums


def _insert_rows(c, table: str, cols_target: list[str], rows: Iterator[dict], ns: str) -> tuple[int, int, list[str]]:
    """-> (inserted, conflicts, dropped columns). A row colliding with a KH-wide unique key (e.g. another
    namespace's client id) is skipped and counted as a conflict."""
    n, conflicts, dropped = 0, 0, set()
    for d in rows:
        row = {"ns": ns}
        for k, v in d.items():
            if k in cols_target:
                row[k] = _denorm(v)
            else:
                dropped.add(k)
        if sqldb.insert_new(c, table, row):
            n += 1
        else:
            conflicts += 1
    return n, conflicts, sorted(dropped)


def _reembed(ns: str, vectors: dict[str, list[dict]]) -> dict:
    from app import vector_store as vs
    from app.embeddings import encode

    out = {}
    for suffix, rows in vectors.items():
        coll = vs.get_or_create(f"ns_{ns}_{suffix}")
        for k in range(0, len(rows), 64):
            part = rows[k:k + 64]
            texts = [r.get("text") or "" for r in part]
            vecs = encode(texts)
            vs.upsert_rows(coll, [r["id"] for r in part], vecs, texts, [r.get("metadata") or {} for r in part])
        out[suffix] = len(rows)
    return out


def _rebuild(ns: str, vectors: dict[str, list[dict]]) -> dict:
    res = {"vectors": _reembed(ns, vectors), "modules": []}
    for mod in _loaded():
        fn = getattr(mod, "rebuild_ns", None)
        if callable(fn):
            try:
                fn(ns)
                res["modules"].append(getattr(mod, "NAME", ""))
            except Exception as e:  # noqa: BLE001
                log.warning("rebuild %s for %s failed: %s", getattr(mod, "NAME", ""), ns, e)
    with sqldb.connect() as c:
        r = c.execute("SELECT config_json FROM kh_ns WHERE ns=?", (ns,)).fetchone()
        if r is not None:
            conf = core.loads(r[0], {}) or {}
            conf.pop("rebuild_pending", None)
            c.execute("UPDATE kh_ns SET config_json=? WHERE ns=?", (core.dumps(conf), ns))
    core.invalidate_ns(ns)
    return res


def do_import(fh, target: str | None, rebuild: str) -> dict:
    if rebuild not in ("sync", "async"):
        raise CapError("invalid", "rebuild must be sync or async")
    try:
        zf = zipfile.ZipFile(fh)
    except zipfile.BadZipFile:
        raise CapError("invalid", "the body is not a zip file")
    with zf:
        manifest, _ = _verify(zf)
        src = manifest.get("ns")
        nsrow = dict(manifest.get("namespace") or {})
        ns = (target or src or "").strip()
        if not core.ns_valid(ns):
            raise CapError("invalid", f"namespace id must match {core.NS_RE.pattern}")
        if ns != src:
            core.check_new_ns(ns)
        projects = core.loads(nsrow.get("projects_json"), []) or []
        caller = core.admin_person(projects, None)
        with sqldb.connect() as c:
            cur = c.execute("SELECT state, purge_after_ms FROM kh_ns WHERE ns=?", (ns,)).fetchone()
            if cur is not None and not (cur[0] == "purged" and cur[1] is None):
                raise CapError("duplicate", f"namespace '{ns}' exists; import needs a clean namespace id")
            if cur is not None:
                c.execute("DELETE FROM kh_ns WHERE ns=? AND state='purged' AND purge_after_ms IS NULL", (ns,))
            for t, _cols, _pk in ns_tables(c, True):
                if t != "kh_lease" and c.execute(f"SELECT 1 FROM {t} WHERE ns=? LIMIT 1", (ns,)).fetchone():
                    raise CapError("duplicate", f"namespace '{ns}' still has rows in {t}; purge it first")
            now = sqldb.now_ms(c)
            conf = core.loads(nsrow.get("config_json"), {}) or {}
            conf.pop("stopped_from", None)
            conf["rebuild_pending"] = True
            conf["imported_from"] = {"ns": src, "export_id": manifest.get("export_id")}
            newrow = {k: nsrow.get(k) for k in nsrow if k != "ns"}
            newrow.update({"ns": ns, "state": "active", "config_json": core.dumps(conf), "detached_ms": None,
                           "purge_after_ms": None, "created_ms": nsrow.get("created_ms") or now})
            cols_ns, _ = _columns(c, "kh_ns")
            sqldb.insert_new(c, "kh_ns", {k: v for k, v in newrow.items() if k in cols_ns})
            counts, dropped, skipped, conflicts = {}, {}, [], {}
            known = {t: cols for t, cols, _pk in ns_tables(c, True)}
            for f in manifest.get("files") or []:
                path, table = f.get("path"), f.get("table")
                if not table:
                    continue
                if table == "kh_counter":
                    n = 0
                    for d in _read_lines(zf, path):
                        name = str(d["name"]).replace(":{ns}", f":{ns}")
                        sqldb.upsert(c, "kh_counter", ["name"], {"name": name, "n": int(d["n"])})
                        n += 1
                    counts[table] = n
                    continue
                if table == "kh_event_week":
                    cols, _ = _columns(c, "kh_event_week")
                elif table in known:
                    cols = known[table]
                else:
                    skipped.append(table)
                    continue
                n, nconf, drop = _insert_rows(c, table, cols, _read_lines(zf, path), ns)
                counts[table] = n
                if nconf:
                    conflicts[table] = nconf
                if drop:
                    dropped[table] = drop
            vectors: dict[str, list[dict]] = {}
            for f in manifest.get("files") or []:
                if f.get("collection_suffix"):
                    rows = []
                    for d in _read_lines(zf, f["path"]):
                        meta = d.get("metadata") or {}
                        if meta.get("ns") == src:
                            meta["ns"] = ns
                        rows.append({"id": d["id"], "text": d.get("text"), "metadata": meta})
                    vectors[f["collection_suffix"]] = rows
    core.invalidate_ns(ns)
    # only into _kh: an event in the restored ns would break the byte-identical re-export
    core.emit(core.KH_NS, "kh.ns_state", {"ns": ns, "from": None, "to": "active", "imported_from": src})
    out = {"ns": ns, "source_ns": src, "export_id": manifest.get("export_id"), "tables": counts,
           "skipped_tables": skipped, "dropped_columns": dropped, "conflicts": conflicts, "actor": caller.principal}
    if rebuild == "sync":
        out["rebuild"] = "done"
        out["rebuilt"] = _rebuild(ns, vectors)
    else:
        out["rebuild"] = "queued"
        threading.Thread(target=_rebuild, args=(ns, vectors), daemon=True, name=f"kh-import-{ns}").start()
    return out


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------


def _delete_batch(c, table: str, where: str, params: list, batch: int) -> int:
    if sqldb.dialect(c) == "hana":
        sql = (f'DELETE FROM {table} WHERE "$rowid$" IN (SELECT TOP {int(batch)} "$rowid$" FROM {table} '
               f"WHERE {where})")
    else:
        sql = f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {where} LIMIT {int(batch)})"
    cur = c.execute(sql, params)
    return int(cur.rowcount or 0)


def purge_data(ns: str) -> dict:
    """Delete every ns row (batched transactions), counters, vectors, module extras; leave the tombstone."""
    batch = purge_batch()
    deleted: dict[str, int] = {}
    # Module hooks run first, while their rows still say what to clean (e.g. which <ns>__<project> graphs exist).
    for mod in _loaded():
        fn = getattr(mod, "purge_ns", None)
        if callable(fn):
            try:
                fn(ns)
            except Exception as e:  # noqa: BLE001
                log.warning("purge_ns %s for %s failed: %s", getattr(mod, "NAME", ""), ns, e)
    with sqldb.connect() as c:
        tables = [t for t, _c, _p in ns_tables(c, True)] + ["kh_event_week", "kh_ns_export"]
    for t in tables:
        total = 0
        while True:
            with sqldb.connect() as c:
                if not sqldb.table_exists(c, t):
                    break
                n = _delete_batch(c, t, "ns=?", [ns], batch)
            total += n
            if n < batch:
                break
        deleted[t] = total
    with sqldb.connect() as c:
        names = [n for n, _ in ns_counters(c, ns)]
        for name in names:
            c.execute("DELETE FROM kh_counter WHERE name=?", (name,))
    deleted["kh_counter"] = len(names)
    dropped = []
    from app import vector_store as vs

    for suffix in vector_suffixes(ns):
        if vs.drop_collection(f"ns_{ns}_{suffix}"):
            dropped.append(f"ns_{ns}_{suffix}")
    with sqldb.connect() as c:
        c.execute("UPDATE kh_ns SET purge_after_ms=NULL, manifest_json=NULL, manifest_sha=NULL, config_json=?, "
                  "rev=rev+1 WHERE ns=? AND state='purged'", (core.dumps({"purged_ms": sqldb.now_ms(c)}), ns))
    core.invalidate_ns(ns)
    return {"deleted": deleted, "vectors_dropped": dropped}


def start_purge(ns: str, src_states: tuple = ("detached",)) -> bool:
    """Conditional `detached -> purged` (single runner). purge_after_ms marks the start time until done."""
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        ph = ", ".join("?" for _ in src_states)
        cur = c.execute(f"UPDATE kh_ns SET state='purged', purge_after_ms=?, rev=rev+1 WHERE ns=? AND state IN ({ph})",
                        (now, ns, *src_states))
        if not cur.rowcount or cur.rowcount <= 0:
            return False
        core.bump_leases(c, ns)
        for fn in core.ON_STATE:
            fn(c, ns, "detached", "purged")
    core.invalidate_ns(ns)
    return True


def purge(ns: str, confirm: str | None, force: bool) -> dict:
    with sqldb.connect() as c:
        row = _ns_row(c, ns)
    if row is None or (row.get("state") == "purged" and row.get("purge_after_ms") is None):
        raise CapError("not_found", f"namespace '{ns}' does not exist")
    core.admin_person([], core.decode_ns_row(_RowView(row)))
    if row["state"] != "detached":
        raise CapError("invalid_transition", f"only a detached namespace can be purged (it is {row['state']})",
                       extra={"from": row["state"], "to": "purged"})
    if force:
        if not core.is_maintainer():
            raise CapError("forbidden", "force=true is for KH maintainers")
    elif not confirm:
        raise CapError("precondition_required", "send {confirm: <export token>} from an export, or ?force=true")
    else:
        with sqldb.connect() as c:
            ok = c.execute("SELECT 1 FROM kh_ns_export WHERE ns=? AND token_sha=? AND completed_ms IS NOT NULL",
                           (ns, core.sha256_text(confirm))).fetchone()
        if ok is None:
            raise CapError("invalid", "confirm does not match a completed export of this namespace")
    if not start_purge(ns):
        raise CapError("invalid_transition", "the namespace changed state concurrently; retry")
    res = purge_data(ns)
    ev = {"ns": ns, "from": "detached", "to": "purged", "forced": bool(force)}
    core.emit(core.KH_NS, "kh.ns_state", ev)
    return {"ns": ns, "state": "purged", **res}


def cron_ns_purge() -> list[str]:
    done = []
    with sqldb.connect() as c:
        if not sqldb.table_exists(c, "kh_ns"):
            return []
        now = sqldb.now_ms(c)
        due = [r[0] for r in c.execute("SELECT ns FROM kh_ns WHERE state='detached' AND purge_after_ms IS NOT NULL "
                                       "AND purge_after_ms<?", (now,)).fetchall()]
        stuck = [r[0] for r in c.execute("SELECT ns FROM kh_ns WHERE state='purged' AND purge_after_ms IS NOT NULL "
                                         "AND purge_after_ms<?", (now - RESUME_PURGE_AFTER_MS,)).fetchall()]
    for ns in due:
        if start_purge(ns):
            purge_data(ns)
            core.emit(core.KH_NS, "kh.ns_state", {"ns": ns, "from": "detached", "to": "purged", "reason": "grace"})
            done.append(ns)
    for ns in stuck:
        purge_data(ns)
        done.append(ns)
    return done


# ---------------------------------------------------------------------------
# Retention sweep
# ---------------------------------------------------------------------------


def week_of(ts_ms: int) -> str:
    y, w, _ = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def sweep_kind(ns: str, kind: str, cutoff_ms: int) -> int:
    """Count events of (ns, kind) older than cutoff into kh_event_week and delete them, batch by batch."""
    total = 0
    batch = purge_batch()
    while True:
        with sqldb.connect() as c:
            rows = c.execute(f"SELECT seq, project, ts_ms FROM kh_event WHERE ns=? AND kind=? AND ts_ms<? "
                             f"ORDER BY seq LIMIT {batch}", (ns, kind, cutoff_ms)).fetchall()
            if not rows:
                return total
            weeks: dict[tuple, int] = {}
            for _seq, project, ts in rows:
                k = (project or "", week_of(ts))
                weeks[k] = weeks.get(k, 0) + 1
            top = int(rows[-1][0])
            cur = c.execute("DELETE FROM kh_event WHERE ns=? AND kind=? AND ts_ms<? AND seq<=?",
                            (ns, kind, cutoff_ms, top))
            if int(cur.rowcount or 0) != len(rows):
                raise RuntimeError(f"retention sweep {ns}/{kind}: concurrent sweeper; rolled back")
            for (project, week), n in weeks.items():
                upd = c.execute("UPDATE kh_event_week SET n=n+? WHERE ns=? AND project=? AND kind=? AND week=?",
                                (n, ns, project, kind, week))
                if not upd.rowcount or upd.rowcount <= 0:
                    sqldb.insert_new(c, "kh_event_week", {"ns": ns, "project": project, "kind": kind, "week": week,
                                                          "n": n})
        total += len(rows)
        if len(rows) < batch:
            return total


def _kinds(c, ns: str) -> list[str]:
    return [r[0] for r in c.execute("SELECT DISTINCT kind FROM kh_event WHERE ns=?", (ns,)).fetchall()]


def ns_retention(c, ns: str, now: int) -> dict[str, int | None]:
    """kind -> cutoff ms (None = keep)."""
    st = core.load_ns(c, ns)
    if st is None:
        return {}
    ret = (st.config.get("retention") or {})
    default_days = ret.get("events_days", config._int("KH_EVENTS_RETENTION_DAYS", 180)) \
        if isinstance(ret, dict) else 180
    out: dict[str, int | None] = {}
    for kind in _kinds(c, ns):
        t = st.types.get(kind)
        if t is not None and t.get("kind_of") == "event":
            days = t.get("retention_days")
        else:
            days = default_days
        out[kind] = None if days is None else now - int(days) * DAY_MS
    return out


def cron_retention() -> dict:
    swept: dict[str, int] = {}
    with sqldb.connect() as c:
        if not sqldb.table_exists(c, "kh_event"):
            return {}
        now = sqldb.now_ms(c)
        plan: list[tuple[str, str, int]] = []
        if sqldb.table_exists(c, "kh_ns"):
            for (ns,) in c.execute("SELECT ns FROM kh_ns WHERE state IN ('active','suspended')").fetchall():
                for kind, cut in ns_retention(c, ns, now).items():
                    if cut is not None:
                        plan.append((ns, kind, cut))
        from app.eventing.emit import namespace as events_ns

        kh_ns = events_ns()
        days = config._int("KH_EVENTS_RETENTION_DAYS", 180)
        prompt_days = config._int("KH_EVENTS_PROMPT_DAYS", 14)
        for own in dict.fromkeys([kh_ns, core.KH_NS]):
            for kind in _kinds(c, own):
                d = prompt_days if (own == kh_ns and kind == PROMPT_KIND) else days
                plan.append((own, kind, now - d * DAY_MS))
    for ns, kind, cut in plan:
        try:
            n = sweep_kind(ns, kind, cut)
        except Exception as e:  # noqa: BLE001
            log.warning("retention %s/%s: %s", ns, kind, e)
            continue
        if n:
            swept[f"{ns}/{kind}"] = n
    return swept


CRONS = [("cron.retention", 3600, cron_retention), ("cron.ns_purge", 3600, cron_ns_purge)]


# ---------------------------------------------------------------------------
# Global stop / resume
# ---------------------------------------------------------------------------


def _require_maintainer() -> None:
    from app import auth

    p = auth.current()
    if p is None and auth.enabled():
        raise CapError("unauthorized", "sign-in required")
    if not core.is_maintainer():
        raise CapError("forbidden", "KH maintainers only")


def stop_all() -> dict:
    _require_maintainer()
    stopped = []
    with sqldb.connect() as c:
        for r in c.execute("SELECT ns, config_json FROM kh_ns WHERE state='active' ORDER BY ns").fetchall():
            ns = r[0]
            if ns in STOP_EXEMPT:
                continue
            conf = core.loads(r[1], {}) or {}
            conf["stopped_from"] = "active"
            cur = c.execute("UPDATE kh_ns SET state='suspended', config_json=?, rev=rev+1 WHERE ns=? AND state='active'",
                            (core.dumps(conf), ns))
            if not cur.rowcount or cur.rowcount <= 0:
                continue
            core.bump_leases(c, ns)
            for fn in core.ON_STATE:
                fn(c, ns, "active", "suspended")
            stopped.append(ns)
    for ns in stopped:
        core.invalidate_ns(ns)
        core.emit(ns, "kh.ns_state", {"ns": ns, "from": "active", "to": "suspended", "reason": "agents_stop"})
    core.emit(core.KH_NS, "kh.agents_stop", {"stopped": stopped})
    return {"stopped": stopped}


def resume_all() -> dict:
    _require_maintainer()
    resumed = []
    with sqldb.connect() as c:
        for r in c.execute("SELECT ns, config_json FROM kh_ns WHERE state='suspended' ORDER BY ns").fetchall():
            ns = r[0]
            conf = core.loads(r[1], {}) or {}
            prior = conf.pop("stopped_from", None)
            if not prior:
                continue
            cur = c.execute("UPDATE kh_ns SET state=?, config_json=?, rev=rev+1 WHERE ns=? AND state='suspended'",
                            (prior, core.dumps(conf), ns))
            if not cur.rowcount or cur.rowcount <= 0:
                continue
            core.bump_leases(c, ns)
            for fn in core.ON_STATE:
                fn(c, ns, "suspended", prior)
            resumed.append(ns)
    for ns in resumed:
        core.invalidate_ns(ns)
        core.emit(ns, "kh.ns_state", {"ns": ns, "from": "suspended", "to": "active", "reason": "agents_resume"})
    core.emit(core.KH_NS, "kh.agents_resume", {"resumed": resumed})
    return {"resumed": resumed}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def _read_limited(request: Request, limit: int):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > limit:
        raise CapError("too_large", f"the export is larger than {limit} bytes", extra={"limit": limit})
    fh = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    n = 0
    async for chunk in request.stream():
        n += len(chunk)
        if n > limit:
            fh.close()
            raise CapError("too_large", f"the export is larger than {limit} bytes", extra={"limit": limit})
        fh.write(chunk)
    fh.seek(0)
    return fh


def attach(app: FastAPI) -> None:
    tag = ["v1 lifecycle"]
    before = len(app.router.routes)

    @app.get("/v1/admin/ns/{ns:ns}:export", tags=tag, summary="Export a namespace (zip)")
    async def export_ns(ns: str, include_vault: bool = Query(False)):
        fh, export_id, token = await core.run(build_export, ns, include_vault)
        return StreamingResponse(_iter_file(fh), media_type="application/zip",
                                 headers={"X-KH-Export-Id": export_id, "X-KH-Export-Token": token,
                                          "Content-Disposition": f'attachment; filename="{ns}-{export_id}.zip"'})

    @app.post("/v1/admin/ns:import", tags=tag, summary="Import a namespace export (zip body)")
    async def import_ns(request: Request, ns: str | None = Query(None), rebuild: str = Query("sync")):
        fh = await _read_limited(request, import_max_bytes())
        try:
            out = await core.run(do_import, fh, ns, rebuild)
        finally:
            fh.close()
        return core.reply(out, status=201)

    @app.post("/v1/admin/ns/{ns:ns}:purge", tags=tag, summary="Purge a detached namespace")
    async def purge_ns(request: Request, ns: str, force: bool = Query(False)):
        body = await core.read_json(request, required=False)
        confirm = body.get("confirm") if isinstance(body, dict) else None
        return await core.run(purge, ns, confirm, force)

    @app.post("/v1/admin/agents:stop", tags=tag, summary="Suspend every active namespace (global stop)")
    async def agents_stop():
        return await core.run(stop_all)

    @app.post("/v1/admin/agents:resume", tags=tag, summary="Resume namespaces the global stop suspended")
    async def agents_resume():
        return await core.run(resume_all)

    # admin verb routes go first so no `/v1/{ns}/{...}` pattern of another module can shadow them
    mine = app.router.routes[before:]
    del app.router.routes[before:]
    app.router.routes[0:0] = mine
