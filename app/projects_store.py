"""Project registry table `projects` (spec §1.5, §2.8). Per-row writes; `save_all` only for the legacy import.

Entry dict: {id, role, aliases[], keywords[], tools[], conflicts_with[], descriptor, paths{}, source,
created_at, updated_at}.
"""
from __future__ import annotations

import json
import logging
import os
import time

from app import config, sqldb

log = logging.getLogger(__name__)

TABLE = "projects"
LIST_FIELDS = ("aliases", "keywords", "tools", "conflicts_with")
_ready_loc: str | None = None


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_table(c) -> None:
    global _ready_loc
    loc = sqldb._location(c)
    if _ready_loc == loc:
        return
    sqldb.create_table(
        c, TABLE,
        "CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT 'project', "
        "aliases TEXT, keywords TEXT, tools TEXT, conflicts_with TEXT, descriptor TEXT, paths TEXT, "
        "created_at TEXT, updated_at TEXT)",
        "CREATE COLUMN TABLE projects (id NVARCHAR(256) PRIMARY KEY, role NVARCHAR(64) DEFAULT 'project', "
        "aliases NCLOB, keywords NCLOB, tools NCLOB, conflicts_with NCLOB, descriptor NCLOB, paths NCLOB, "
        "created_at NVARCHAR(32), updated_at NVARCHAR(32))",
    )
    sqldb.add_column(c, TABLE, "source", "TEXT DEFAULT 'KH'", "NVARCHAR(64) DEFAULT 'KH'")
    _ready_loc = loc
    _import_legacy_manifest(c)


def _jl(v) -> list:
    if not v:
        return []
    try:
        x = json.loads(v)
        return x if isinstance(x, list) else []
    except ValueError:
        return []


def _jo(v) -> dict:
    if not v:
        return {}
    try:
        x = json.loads(v)
        return x if isinstance(x, dict) else {}
    except ValueError:
        return {}


def _from_row(r) -> dict:
    return {
        "id": r["id"],
        "role": r["role"] or "project",
        "aliases": _jl(r["aliases"]),
        "keywords": _jl(r["keywords"]),
        "tools": _jl(r["tools"]),
        "conflicts_with": _jl(r["conflicts_with"]),
        "descriptor": r["descriptor"] or "",
        "paths": _jo(r["paths"]),
        "source": (r["source"] if "source" in r.keys() else None) or "KH",
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def _to_row(e: dict) -> dict:
    return {
        "id": e["id"],
        "role": e.get("role") or "project",
        "aliases": json.dumps(list(e.get("aliases") or []), ensure_ascii=False),
        "keywords": json.dumps(list(e.get("keywords") or []), ensure_ascii=False),
        "tools": json.dumps(list(e.get("tools") or []), ensure_ascii=False),
        "conflicts_with": json.dumps(list(e.get("conflicts_with") or []), ensure_ascii=False),
        "descriptor": e.get("descriptor") or "",
        "paths": json.dumps(e.get("paths") or {}, ensure_ascii=False),
        "source": (e.get("source") or "KH")[:64],
        "created_at": e.get("created_at") or now_iso(),
        "updated_at": e.get("updated_at") or now_iso(),
    }


_COLS = "id, role, aliases, keywords, tools, conflicts_with, descriptor, paths, source, created_at, updated_at"


def get_one(pid: str) -> dict | None:
    with sqldb.connect() as c:
        ensure_table(c)
        r = c.execute(f"SELECT {_COLS} FROM projects WHERE id=?", (pid,)).fetchone()
    return _from_row(r) if r else None


def load_all() -> dict[str, dict]:
    with sqldb.connect() as c:
        ensure_table(c)
        rows = c.execute(f"SELECT {_COLS} FROM projects ORDER BY id").fetchall()
    return {r["id"]: _from_row(r) for r in rows}


def upsert_one(entry: dict) -> None:
    with sqldb.connect() as c:
        ensure_table(c)
        sqldb.upsert(c, TABLE, ["id"], _to_row(entry))


def delete_one(pid: str) -> bool:
    with sqldb.connect() as c:
        ensure_table(c)
        cur = c.execute("DELETE FROM projects WHERE id=?", (pid,))
        return bool(cur.rowcount and cur.rowcount > 0)


def save_all(entries: dict[str, dict], c=None) -> None:
    """Replace the table with `entries` (rows not in the dict are deleted). Legacy import only."""
    def run(conn):
        existing = {r[0] for r in conn.execute("SELECT id FROM projects").fetchall()}
        for pid in existing - set(entries):
            conn.execute("DELETE FROM projects WHERE id=?", (pid,))
        for pid, e in entries.items():
            sqldb.upsert(conn, TABLE, ["id"], _to_row({**e, "id": pid}))

    if c is not None:
        run(c)
    else:
        with sqldb.connect() as conn:
            ensure_table(conn)
            run(conn)


def _import_legacy_manifest(c) -> None:
    """One-time import of `projects_manifest.json`, renamed `.migrated-<UTC stamp>` afterwards."""
    path = os.path.join(config.ROOT, "projects_manifest.json")
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        projects = data.get("projects", data) if isinstance(data, dict) else {}
        entries = {pid: {**(e or {}), "id": pid} for pid, e in projects.items() if isinstance(e, dict)}
        if entries:
            save_all(entries, c)
        os.replace(path, f"{path}.migrated-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
        log.info("imported %d projects from legacy projects_manifest.json", len(entries))
    except Exception as e:  # noqa: BLE001
        log.warning("legacy projects_manifest.json import failed: %s", e)
