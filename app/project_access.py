"""Visibility mirror `project_access` (spec §2.8). Display only: access comes from the token.

Table: id pk, visibility (`private` default | `semi-public` | `public`), updated_at, updated_by. A private project
has no row (setting private deletes it). Reads are cached `KH_PROJECT_ACCESS_CACHE_S` (5 s); an unreadable table
means every project is private. Orchestrator and personal ids cannot change visibility.
"""
from __future__ import annotations

import logging
import threading
import time

from app import config, sqldb

log = logging.getLogger(__name__)

TABLE = "project_access"
VISIBILITIES = ("private", "semi-public", "public")
PRIVATE = "private"

_cache: dict = {"at": 0.0, "map": None}
_lock = threading.Lock()
_ready: set[str] = set()


def ensure_table(c) -> None:
    loc = sqldb._location(c)
    if loc in _ready:
        return
    sqldb.create_table(
        c, TABLE,
        "CREATE TABLE IF NOT EXISTS project_access (id TEXT PRIMARY KEY, visibility TEXT NOT NULL DEFAULT 'private', "
        "updated_at TEXT, updated_by TEXT)",
        "CREATE COLUMN TABLE project_access (id NVARCHAR(256) PRIMARY KEY, visibility NVARCHAR(32) DEFAULT 'private', "
        "updated_at NVARCHAR(32), updated_by NVARCHAR(320))",
    )
    _ready.add(loc)


def _ttl() -> float:
    return config._float("KH_PROJECT_ACCESS_CACHE_S", config.PROJECT_ACCESS_CACHE_S)


def invalidate() -> None:
    with _lock:
        _cache["map"] = None
        _cache["at"] = 0.0


def all_visibility() -> dict[str, str]:
    """{project id: visibility} of every non-private project (cached)."""
    now = time.time()
    with _lock:
        if _cache["map"] is not None and now - _cache["at"] < _ttl():
            return dict(_cache["map"])
    try:
        with sqldb.connect() as c:
            ensure_table(c)
            m = {r["id"]: r["visibility"] for r in c.execute("SELECT id, visibility FROM project_access").fetchall()}
    except Exception as e:  # noqa: BLE001
        log.warning("project_access unreadable (all private): %s", e)
        m = {}
    with _lock:
        _cache["map"] = m
        _cache["at"] = now
    return dict(m)


def visibility(pid: str) -> str:
    return all_visibility().get(pid, PRIVATE)


def fixed(pid: str) -> bool:
    """Orchestrator and personal projects are always private."""
    from app import personal

    return pid == config.ORCHESTRATOR_ID or personal.is_personal(pid)


def set_visibility(pid: str, vis: str, by: str | None = None) -> str:
    if vis not in VISIBILITIES:
        raise ValueError(f"visibility must be one of {', '.join(VISIBILITIES)}")
    if fixed(pid):
        raise ValueError(f"the visibility of '{pid}' is fixed (private)")
    from app import projects_store

    with sqldb.connect() as c:
        ensure_table(c)
        if vis == PRIVATE:
            c.execute("DELETE FROM project_access WHERE id=?", (pid,))
        else:
            sqldb.upsert(c, TABLE, "id", {"id": pid, "visibility": vis, "updated_at": projects_store.now_iso(),
                                          "updated_by": by})
    invalidate()
    return vis


def delete(pid: str) -> dict:
    try:
        with sqldb.connect() as c:
            ensure_table(c)
            n = c.execute("DELETE FROM project_access WHERE id=?", (pid,)).rowcount
    except Exception as e:  # noqa: BLE001
        log.warning("project_access delete %s failed: %s", pid, e)
        n = 0
    invalidate()
    return {"project_access_deleted": bool(n and n > 0)}


def _register_hook() -> None:
    from app import projects

    projects.on_delete(delete)


_register_hook()
