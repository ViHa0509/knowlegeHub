"""Session (actor) -> project binding (spec §2.11).

Table `kh_session_binding`: owner (personal project id of the account), actor, project, source
(`detected` | `asked`), bound_at, last_seen; pk (owner, actor); index on last_seen. A binding lives while it
is used: `get` ignores rows idle longer than `KH_SESSION_BINDING_TTL_HOURS` (72).

Public helpers (used by the auth middleware and by §3 `/turn/context`):
    get(owner, actor) -> binding dict | None
    touch(owner, actor)                  refresh last_seen of a live row, at most once per 600 s per session
    bind(owner, actor, project, source)  -> binding
    unbind(owner, actor) -> bool
    detect(principal, actor, text) -> binding | None   bind `detected` per §2.11 (turn context calls this)
    apply_to_log_write(principal, actor, body) -> bool  rewrite a /vector/add log body to the bound project
    sweep(max_idle_hours) -> {deleted, idle_before}
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import auth, config, sqldb

log = logging.getLogger(__name__)

TABLE = "kh_session_binding"
TOUCH_EVERY_S = 600
_touched: dict[tuple[str, str], float] = {}
_touch_lock = threading.Lock()
_ready: set[str] = set()


def ttl_hours() -> float:
    return config._float("KH_SESSION_BINDING_TTL_HOURS", 72)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_table(c) -> None:
    loc = sqldb._location(c)
    if loc in _ready:
        return
    sqldb.create_table(
        c, TABLE,
        "CREATE TABLE IF NOT EXISTS kh_session_binding (owner TEXT NOT NULL, actor TEXT NOT NULL, project TEXT NOT NULL, "
        "source TEXT, bound_at TEXT, last_seen TEXT, PRIMARY KEY (owner, actor))",
        "CREATE COLUMN TABLE kh_session_binding (owner NVARCHAR(320) NOT NULL, actor NVARCHAR(128) NOT NULL, "
        "project NVARCHAR(256) NOT NULL, source NVARCHAR(16), bound_at NVARCHAR(32), last_seen NVARCHAR(32), "
        "PRIMARY KEY (owner, actor))",
    )
    sqldb.create_index(c, "ix_kh_session_binding_seen", TABLE, "last_seen")
    _ready.add(loc)


def _row(r) -> dict:
    return {"actor": r["actor"], "project": r["project"], "source": r["source"], "bound_at": r["bound_at"],
            "last_seen": r["last_seen"]}


def _cutoff() -> str:
    return _iso(_now() - timedelta(hours=ttl_hours()))


def get(owner: str | None, actor: str | None) -> dict | None:
    if not owner or not actor:
        return None
    with sqldb.connect() as c:
        ensure_table(c)
        r = c.execute("SELECT actor, project, source, bound_at, last_seen FROM kh_session_binding "
                      "WHERE owner=? AND actor=?", (owner, actor)).fetchone()
    if r is None or (r["last_seen"] or "") < _cutoff():
        return None
    return _row(r)


def touch(owner: str | None, actor: str | None) -> None:
    if not owner or not actor:
        return
    key = (owner, actor)
    now = time.time()
    with _touch_lock:
        if now - _touched.get(key, 0.0) < TOUCH_EVERY_S:
            return
        _touched[key] = now
    try:
        with sqldb.connect() as c:
            ensure_table(c)
            c.execute("UPDATE kh_session_binding SET last_seen=? WHERE owner=? AND actor=? AND last_seen>=?",
                      (_iso(_now()), owner, actor, _cutoff()))
    except Exception as e:  # noqa: BLE001
        log.warning("session touch failed: %s", e)


def bind(owner: str, actor: str, project: str, source: str = "asked") -> dict:
    now = _iso(_now())
    with sqldb.connect() as c:
        ensure_table(c)
        sqldb.upsert(c, TABLE, ["owner", "actor"], {"owner": owner, "actor": actor, "project": project,
                                                     "source": source, "bound_at": now, "last_seen": now})
    with _touch_lock:
        _touched[(owner, actor)] = time.time()
    return {"actor": actor, "project": project, "source": source, "bound_at": now, "last_seen": now}


def unbind(owner: str, actor: str) -> bool:
    with sqldb.connect() as c:
        ensure_table(c)
        n = c.execute("DELETE FROM kh_session_binding WHERE owner=? AND actor=?", (owner, actor)).rowcount
    with _touch_lock:
        _touched.pop((owner, actor), None)
    return bool(n and n > 0)


def sweep(max_idle_hours: float | None = None) -> dict:
    hours = ttl_hours() if max_idle_hours is None else float(max_idle_hours)
    before = _iso(_now() - timedelta(hours=hours))
    with sqldb.connect() as c:
        ensure_table(c)
        n = c.execute("DELETE FROM kh_session_binding WHERE last_seen<?", (before,)).rowcount
    return {"deleted": max(int(n or 0), 0), "idle_before": before}


def delete_project_rows(pid: str) -> dict:
    try:
        with sqldb.connect() as c:
            ensure_table(c)
            n = c.execute("DELETE FROM kh_session_binding WHERE project=?", (pid,)).rowcount
    except Exception as e:  # noqa: BLE001
        log.warning("session bindings of %s not removed: %s", pid, e)
        n = 0
    return {"session_bindings_deleted": max(int(n or 0), 0)}


def detect(principal, actor: str | None, text: str) -> dict | None:
    """Bind `detected` when the session is unbound and the prompt names exactly one project by id or alias
    (one strongest hit, no equal-length tie; keywords never bind) on which the caller holds project.log."""
    if principal is None or not actor or getattr(principal, "ns", None):
        return None
    owner = principal.personal_project
    if get(owner, actor) is not None:
        return None
    from app import projects

    hits = [h for h in projects.match_projects(text) if h["matched_on"] in ("id", "alias")]
    if not hits:
        return None
    if len(hits) > 1 and hits[0]["term_length"] == hits[1]["term_length"]:
        return None
    pid = hits[0]["project"]
    if not principal.can(pid, "project.log"):
        return None
    b = bind(owner, actor, pid, "detected")
    b["prompt_names"] = hits[0]["matched_term"]
    return b


def apply_to_log_write(principal, actor: str | None, body: dict) -> bool:
    """POST /vector/add with scope log + actor + live binding + project.log on the bound project: drop
    metadata.source_id and body.project, set metadata.project = bound, metadata.project_from = "session"."""
    if principal is None or not actor or not isinstance(body, dict):
        return False
    if (body.get("scope") or "knowledge") != config.HOTLOG_SCOPE:
        return False
    b = get(principal.personal_project, actor)
    if b is None or not principal.can(b["project"], "project.log"):
        return False
    meta = body.get("metadata")
    if not isinstance(meta, dict):
        meta = {}
    meta.pop("source_id", None)
    body.pop("project", None)
    meta["project"] = b["project"]
    meta["project_from"] = "session"
    body["metadata"] = meta
    return True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class BindBody(BaseModel):
    project: Optional[str] = None


class SweepBody(BaseModel):
    max_idle_hours: Any = None


def _who() -> tuple[Any, str]:
    p = auth.current()
    if p is None or p.ns:
        raise HTTPException(status_code=400, detail="session binding needs sign-in")
    actor = auth.current_actor()
    if not actor:
        raise HTTPException(status_code=400, detail="name the session in the x-kh-actor header")
    return p, actor


def attach(app: FastAPI) -> None:
    from app import projects

    projects.on_delete(delete_project_rows)

    @app.get("/session/project", tags=["session"])
    def session_get():
        p, actor = _who()
        return {"actor": actor, "binding": get(p.personal_project, actor), "ttl_hours": ttl_hours()}

    @app.put("/session/project", tags=["session"])
    def session_put(body: BindBody):
        p, actor = _who()
        pid = projects.canonical_or_none(body.project)
        if not pid or not projects.exists(pid):
            raise HTTPException(status_code=404, detail="no such project")
        if not p.can(pid, "project.log"):
            raise HTTPException(status_code=403, detail="missing permission project.log on this project")
        previous = get(p.personal_project, actor)
        return {"binding": bind(p.personal_project, actor, pid, "asked"), "previous": previous}

    @app.delete("/session/project", tags=["session"])
    def session_delete():
        p, actor = _who()
        return {"actor": actor, "unbound": unbind(p.personal_project, actor)}

    @app.post("/session/sweep", tags=["session"])
    def session_sweep(body: SweepBody | None = None):
        v = body.max_idle_hours if body is not None else None
        if v is not None:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 1:
                raise HTTPException(status_code=400, detail="max_idle_hours must be a number >= 1")
        return sweep(v)
