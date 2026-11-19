"""Flow definitions (seeded from the KH 7.1 build) and the run log (table kh_flow_run)."""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from app import config, sqldb

SEED = Path(config.ROOT) / "seeds" / "flows.seed.json"
RUN_TTL_HOURS = 24
_lock = threading.Lock()
_cache: dict[str, Any] = {}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_flows() -> list[dict]:
    try:
        return list(json.loads(SEED.read_text(encoding="utf-8")).get("flows") or [])
    except (OSError, ValueError):
        return []


def edits() -> dict[str, dict]:
    """Stored flow edits by id (table kh_flow_def); a failing read means no edits."""
    out: dict[str, dict] = {}
    try:
        with sqldb.connect() as c:
            _ensure_def(c)
            for r in c.execute("SELECT id, body FROM kh_flow_def").fetchall():
                try:
                    out[r["id"]] = json.loads(r["body"] or "{}")
                except ValueError:
                    continue
    except Exception:  # noqa: BLE001
        return {}
    return out


def flows() -> list[dict]:
    with _lock:
        if "flows" in _cache:
            return _cache["flows"]
    seed = _seed_flows()
    ed = edits()
    merged = []
    for f in seed:
        e = ed.pop(f["id"], None)
        if e is None:
            merged.append(f)
        elif not e.get("deleted"):
            merged.append(e)
    merged.extend(e for e in ed.values() if not e.get("deleted") and e.get("id"))
    raw = json.dumps(merged, sort_keys=True).encode()
    with _lock:
        _cache["flows"] = merged
        _cache["rev"] = hashlib.sha1(raw).hexdigest()[:12]
        return merged


def invalidate() -> None:
    """Drop the merged flow list and the cached search vectors (after a save or delete)."""
    with _lock:
        _cache.clear()


def rev() -> str:
    fl = flows()
    with _lock:
        if "rev" not in _cache:
            _cache["rev"] = hashlib.sha1(json.dumps(fl, sort_keys=True).encode()).hexdigest()[:12]
        return _cache["rev"]


def seed_flow(flow_id: str) -> Optional[dict]:
    for f in _seed_flows():
        if f.get("id") == flow_id:
            return f
    return None


_DEF_SQLITE = ("CREATE TABLE IF NOT EXISTS kh_flow_def (id TEXT PRIMARY KEY, body TEXT, updated TEXT, actor TEXT)")
_DEF_HANA = ("CREATE COLUMN TABLE kh_flow_def (id NVARCHAR(200) PRIMARY KEY, body NCLOB, updated NVARCHAR(32), "
             "actor NVARCHAR(200))")
_TYPE_SQLITE = ("CREATE TABLE IF NOT EXISTS kh_project_type (id TEXT PRIMARY KEY, body TEXT, updated TEXT, "
                "actor TEXT)")
_TYPE_HANA = ("CREATE COLUMN TABLE kh_project_type (id NVARCHAR(200) PRIMARY KEY, body NCLOB, "
              "updated NVARCHAR(32), actor NVARCHAR(200))")


def _ensure_def(c) -> None:
    sqldb.create_table(c, "kh_flow_def", _DEF_SQLITE, _DEF_HANA)


def _ensure_type(c) -> None:
    sqldb.create_table(c, "kh_project_type", _TYPE_SQLITE, _TYPE_HANA)


def _put(table: str, ensure, key: str, body: dict, actor: str) -> None:
    with sqldb.connect() as c:
        ensure(c)
        sqldb.upsert(c, table, "id", {"id": key, "body": json.dumps(body, ensure_ascii=False),
                                      "updated": _iso(datetime.now(timezone.utc)), "actor": actor})


def save_flow(flow_id: str, body: dict, actor: str) -> dict:
    """Store an edited (or new) flow over the seed; the version goes up by one."""
    prev = get(flow_id)
    f = dict(body)
    f["id"] = flow_id
    f["version"] = int((prev or {}).get("version") or 0) + 1
    _put("kh_flow_def", _ensure_def, flow_id, f, actor)
    invalidate()
    return f


def delete_flow(flow_id: str, actor: str) -> str:
    """Drop the edit: a seed flow returns to its seed version ('reset'), a new flow goes ('deleted')."""
    seed = seed_flow(flow_id)
    if seed is not None:
        if seed.get("kind") == "system":
            return "system"
        with sqldb.connect() as c:
            _ensure_def(c)
            had = c.execute("SELECT id FROM kh_flow_def WHERE id = ?", (flow_id,)).fetchone()
            c.execute("DELETE FROM kh_flow_def WHERE id = ?", (flow_id,))
        invalidate()
        return "reset" if had else "missing"
    with sqldb.connect() as c:
        _ensure_def(c)
        had = c.execute("SELECT id FROM kh_flow_def WHERE id = ?", (flow_id,)).fetchone()
        c.execute("DELETE FROM kh_flow_def WHERE id = ?", (flow_id,))
    invalidate()
    return "deleted" if had else "missing"


def type_edits() -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        with sqldb.connect() as c:
            _ensure_type(c)
            for r in c.execute("SELECT id, body FROM kh_project_type").fetchall():
                try:
                    out[r["id"]] = json.loads(r["body"] or "{}")
                except ValueError:
                    continue
    except Exception:  # noqa: BLE001
        return {}
    return out


def save_type(type_id: str, body: dict, actor: str) -> dict:
    t = dict(body)
    t["id"] = type_id
    _put("kh_project_type", _ensure_type, type_id, t, actor)
    return t


def delete_type(type_id: str, seed_ids: set[str]) -> str:
    with sqldb.connect() as c:
        _ensure_type(c)
        had = c.execute("SELECT id FROM kh_project_type WHERE id = ?", (type_id,)).fetchone()
        c.execute("DELETE FROM kh_project_type WHERE id = ?", (type_id,))
    if not had:
        return "missing"
    return "reset" if type_id in seed_ids else "deleted"


def get(flow_id: str) -> Optional[dict]:
    for f in flows():
        if f.get("id") == flow_id:
            return f
    return None


def listing(f: dict) -> dict:
    return {"id": f["id"], "name": f.get("name"), "description": f.get("description") or f.get("desc") or "",
            "kind": f.get("kind"), "version": f.get("version"), "enabled": f.get("enabled", True),
            "triggers": f.get("triggers") or [], "input": f.get("input")}


# ---------------------------------------------------------------------------
# Flow search by meaning (GET /flows?q=)
# ---------------------------------------------------------------------------


def _prompt_trigger(f: dict) -> Optional[dict]:
    for t in f.get("triggers") or []:
        if t.get("type") == "prompt":
            return t
    return None


def _flow_text(f: dict) -> str:
    return f"{f.get('name') or ''}. {f.get('description') or f.get('desc') or ''}"


def _vectors(model) -> dict[str, Any]:
    """Per flow: (description vector, example vectors, not-example vectors), cached per model."""
    key = ("vecs", id(model))
    with _lock:
        if key in _cache:
            return _cache[key]
    out = {}
    for f in flows():
        if f.get("kind") == "system" or not f.get("enabled", True):
            continue
        t = _prompt_trigger(f) or {}
        ex = [str(x) for x in t.get("examples") or []]
        nots = [str(x) for x in t.get("not") or []]
        texts = [_flow_text(f)] + ex + nots
        vecs = model.encode(texts, normalize_embeddings=True)
        out[f["id"]] = (vecs[0], vecs[1:1 + len(ex)], vecs[1 + len(ex):])
    with _lock:
        _cache[key] = out
    return out


def _dot(a, b) -> float:
    return float(sum(float(x) * float(y) for x, y in zip(a, b)))


def search(query: str, k: int, model) -> list[dict]:
    qv = model.encode(query, normalize_embeddings=True)
    scored = []
    for fid, (dv, exs, nots) in _vectors(model).items():
        s = max([_dot(qv, dv)] + [_dot(qv, e) for e in exs])
        neg = max([_dot(qv, n) for n in nots] or [0.0])
        f = get(fid)
        row = listing(f)
        row["score"] = round(s, 3)
        row["refused"] = neg > s and neg >= FLOW_MATCH_FLOOR  # a 'not' example refuses only as a confident match
        t = _prompt_trigger(f) or {}
        row["default"] = bool(t.get("default"))
        scored.append(row)
    scored.sort(key=lambda r: -r["score"])
    return scored[:max(1, k)]


TASK_FLOW_FLOOR = config._float("KH_TASK_FLOW_FLOOR", 0.45)  # the default flow (task.full-retrieve)
FLOW_MATCH_FLOOR = 0.66  # KH_FLOW_MATCH_FLOOR (app/thresholds.py): any other flow


def nearest_task_flow(text: str, model) -> Optional[dict]:
    """The flow the prompt hook names (TASK FLOW line), or None."""
    for r in search(text, 3, model):
        floor = TASK_FLOW_FLOOR if r["default"] else FLOW_MATCH_FLOOR
        if r["refused"] or r["score"] < floor or not _prompt_trigger(get(r["id"]) or {}):
            continue
        return {"id": r["id"], "score": r["score"], "default": r["default"]}
    return None


# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

_DDL_SQLITE = ("CREATE TABLE IF NOT EXISTS kh_flow_run (run_id TEXT PRIMARY KEY, flow TEXT, version INTEGER, "
               "status TEXT, created TEXT, expires TEXT, origin TEXT, event TEXT, caller TEXT, ms INTEGER, "
               "input TEXT, outcome TEXT, message TEXT, result TEXT)")
_DDL_HANA = ("CREATE COLUMN TABLE kh_flow_run (run_id NVARCHAR(64) PRIMARY KEY, flow NVARCHAR(200), version INTEGER, "
             "status NVARCHAR(32), created NVARCHAR(32), expires NVARCHAR(32), origin NVARCHAR(32), "
             "event NVARCHAR(200), caller NVARCHAR(200), ms INTEGER, input NCLOB, outcome NVARCHAR(32), "
             "message NCLOB, result NCLOB)")
_ready = {"ok": False}


def _ensure(c) -> None:
    if not _ready["ok"]:
        sqldb.create_table(c, "kh_flow_run", _DDL_SQLITE, _DDL_HANA)
        _ready["ok"] = True


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:16]


def record(run_id: str, flow: dict, inp: dict, result: dict, ms: int, caller: str, origin: str = "request",
           event: Optional[str] = None) -> None:
    now = datetime.now(timezone.utc)
    row = {"run_id": run_id, "flow": flow["id"], "version": int(flow.get("version") or 1),
           "status": result.get("status") or "done", "created": _iso(now),
           "expires": _iso(now + timedelta(hours=RUN_TTL_HOURS)), "origin": origin, "event": event,
           "caller": caller, "ms": int(ms), "input": json.dumps(inp, ensure_ascii=False)[:20000],
           "outcome": result.get("outcome") or "done", "message": result.get("message") or "",
           "result": json.dumps(result, ensure_ascii=False, default=str)[:200000]}
    try:
        with sqldb.connect() as c:
            _ensure(c)
            sqldb.upsert(c, "kh_flow_run", "run_id", row)
    except Exception:  # noqa: BLE001  (a run log failure never fails the run)
        pass


def _run_out(r, with_result: bool) -> dict:
    out = {"run_id": r["run_id"], "flow": r["flow"], "version": r["version"], "status": r["status"],
           "created": r["created"], "expires": r["expires"], "origin": r["origin"], "event": r["event"],
           "depth": None, "caller": r["caller"], "ms": r["ms"], "input": json.loads(r["input"] or "{}"),
           "outcome": r["outcome"], "message": r["message"]}
    if with_result:
        try:
            out["result"] = json.loads(r["result"] or "{}")
        except ValueError:
            out["result"] = {}
    return out


def runs(limit: int = 50, flow: Optional[str] = None) -> list[dict]:
    with sqldb.connect() as c:
        _ensure(c)
        now = _iso(datetime.now(timezone.utc))
        sql = "SELECT * FROM kh_flow_run WHERE expires > ?"
        params: list = [now]
        if flow:
            sql += " AND flow = ?"
            params.append(flow)
        sql += " ORDER BY created DESC"
        rows = c.execute(sql, params).fetchall()
    return [_run_out(r, False) for r in rows[:limit]]


def run(run_id: str) -> Optional[dict]:
    with sqldb.connect() as c:
        _ensure(c)
        r = c.execute("SELECT * FROM kh_flow_run WHERE run_id = ?", (run_id,)).fetchone()
    return _run_out(r, True) if r else None


def now_ms() -> float:
    return time.perf_counter() * 1000.0
