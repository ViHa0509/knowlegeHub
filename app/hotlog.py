"""Hot log window (spec §1.9): one shared partition `kh_hotlog` in front of the per-project `kh_log_<id>`.

Public names:
    SCOPE, enabled(), is_windowed(scope), collection()
    parse_ts(value) -> float epoch seconds (0.0 = unknown / oldest)
    slots(author=None) -> {project: [(id, ts)] newest first}
    enforce(author=None, may_log=None) -> {enabled, spilled:{project:n}, slots}
    read_lane(scope, project, base_where) -> (collection, where) | None
    recent_where(project, caller, base_where) -> where
    delete_project_rows(pid) -> int
    window(author=None, visible=None) -> /log/window body
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from app import config

log = logging.getLogger(__name__)

SCOPE = config.HOTLOG_SCOPE
COLLECTION = config.HOTLOG_COLLECTION
_enforce_lock = threading.Lock()


def enabled() -> bool:
    return bool(config.HOTLOG_ENABLED)


def is_windowed(scope: str | None) -> bool:
    return bool(scope) and scope == SCOPE


def collection():
    from app import vector_store as vs

    return vs.get_or_create(COLLECTION)


def parse_ts(value: Any) -> float:
    """Number or numeric string = epoch seconds; ISO-8601 (Z -> +00:00, naive = UTC); else 0.0."""
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        pass
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def slots(author: str | None = None) -> dict[str, list[tuple[str, float]]]:
    """Per-project slot contents (metadata only). Untagged rows are ignored."""
    from app import vector_store as vs

    where = {"author": {"$eq": author}} if author else None
    out: dict[str, list[tuple[str, float]]] = {}
    for rid, m in vs.meta_rows_vt(collection(), where):
        p = m.get("project")
        if not p:
            continue
        out.setdefault(p, []).append((rid, parse_ts(m.get("ts"))))
    for p in out:
        out[p].sort(key=lambda t: t[1], reverse=True)  # stable: equal ts keep insertion order
    return out


def _spill(project: str, ids: list[str], may_log: Callable[[str], bool] | None) -> int:
    from app import vector_store as vs

    if not ids:
        return 0
    hot = collection()
    if may_log is not None and not may_log(project):
        hot.delete(ids=ids)
        return len(ids)
    dst = vs.get_or_create(vs.part_collection_name(SCOPE, project))
    return vs.move_rows(ids, hot, dst)


def enforce(author: str | None = None, may_log: Callable[[str], bool] | None = None) -> dict:
    """Spill (AGE, DEPTH, SLOTS) into kh_log_<project>; rows are dropped where may_log(project) is False."""
    spilled: dict[str, int] = {}

    def add(p, n):
        if n:
            spilled[p] = spilled.get(p, 0) + n

    with _enforce_lock:
        sl = slots(author)
        if not enabled():
            for p, rows in sl.items():
                add(p, _spill(p, [r for r, _ in rows], may_log))
            return {"enabled": False, "spilled": spilled, "slots": 0}
        # 1. AGE
        cutoff = time.time() - float(config.HOTLOG_MAX_AGE_DAYS) * 86400.0
        for p in list(sl):
            old = [r for r, ts in sl[p] if 0 < ts < cutoff]
            if old:
                add(p, _spill(p, old, may_log))
                sl[p] = [(r, ts) for r, ts in sl[p] if r not in set(old)]
                if not sl[p]:
                    del sl[p]
        # 2. DEPTH
        cap = max(0, int(config.HOTLOG_MAX_PER_SLOT))
        for p in list(sl):
            if len(sl[p]) > cap:
                extra = [r for r, _ in sl[p][cap:]]
                add(p, _spill(p, extra, may_log))
                sl[p] = sl[p][:cap]
                if not sl[p]:
                    del sl[p]
        # 3. SLOTS (LRU by newest entry)
        nslots = max(0, int(config.HOTLOG_SLOTS))
        if len(sl) > nslots:
            order = sorted(sl, key=lambda p: max(ts for _, ts in sl[p]), reverse=True)
            for p in order[nslots:]:
                add(p, _spill(p, [r for r, _ in sl[p]], may_log))
                del sl[p]
        return {"enabled": True, "spilled": spilled, "slots": len(sl)}


def read_lane(scope: str | None, project: str | None, base_where: dict | None):
    """Hot-window lane for the windowed scope, regardless of the on/off flag."""
    if not is_windowed(scope):
        return None
    from app import auth
    from app import vector_store as vs

    if project:
        narrow = {"project": {"$eq": project}}
    else:
        visible = auth.visible_projects("project.read")
        if visible is None:
            narrow = None
        else:
            narrow = {"project": {"$in": sorted(visible) or ["__no_visible_project__"]}}
    if not vs.collection_exists(COLLECTION):
        return None
    return collection(), vs.and_where(base_where, narrow)


def recent_where(project: str | None, caller: str | None, base_where: dict | None = None) -> dict | None:
    """No project named -> own window only (author == caller); project named -> every author."""
    from app import vector_store as vs

    if project or not caller:
        return base_where
    return vs.and_where(base_where, {"author": {"$eq": caller}})


def delete_project_rows(pid: str) -> int:
    from app import vector_store as vs

    if not vs.collection_exists(COLLECTION):
        return 0
    hot = collection()
    ids = hot.get(where={"project": {"$eq": pid}}, include=[]).get("ids") or []
    if ids:
        hot.delete(ids=ids)
    return len(ids)


def window(author: str | None = None, visible: set | None = None) -> dict:
    """Body of GET /log/window (caller's own window, restricted to visible projects)."""
    from app import vector_store as vs

    out_slots = []
    if vs.collection_exists(COLLECTION):
        where = {"author": {"$eq": author}} if author else None
        newest: dict[str, tuple[float, Any]] = {}
        counts: dict[str, int] = {}
        for _rid, m in vs.meta_rows_vt(collection(), where):
            p = m.get("project")
            if not p or (visible is not None and p not in visible):
                continue
            counts[p] = counts.get(p, 0) + 1
            ts = parse_ts(m.get("ts"))
            if p not in newest or ts > newest[p][0]:
                newest[p] = (ts, m.get("ts"))
        for p in counts:
            out_slots.append({"project": p, "entries": counts[p], "newest_ts": newest[p][1]})
        out_slots.sort(key=lambda s: newest[s["project"]][0], reverse=True)
    return {
        "enabled": enabled(),
        "scope": SCOPE,
        "collection": COLLECTION,
        "caps": {"slots": config.HOTLOG_SLOTS, "max_per_slot": config.HOTLOG_MAX_PER_SLOT,
                 "max_age_days": config.HOTLOG_MAX_AGE_DAYS},
        "slots_used": len(out_slots),
        "slots": out_slots,
    }
