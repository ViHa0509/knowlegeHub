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


def flows() -> list[dict]:
    with _lock:
        if "flows" not in _cache:
            try:
                data = json.loads(SEED.read_text(encoding="utf-8"))
                _cache["flows"] = list(data.get("flows") or [])
            except (OSError, ValueError):
                _cache["flows"] = []
            raw = json.dumps(_cache["flows"], sort_keys=True).encode()
            _cache["rev"] = hashlib.sha1(raw).hexdigest()[:12]
        return _cache["flows"]


def rev() -> str:
    flows()
    return _cache["rev"]


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
        row["refused"] = neg > s
        t = _prompt_trigger(f) or {}
        row["default"] = bool(t.get("default"))
        scored.append(row)
    scored.sort(key=lambda r: -r["score"])
    return scored[:max(1, k)]


TASK_FLOW_FLOOR = config._float("KH_TASK_FLOW_FLOOR", 0.45)


def nearest_task_flow(text: str, model) -> Optional[dict]:
    """The flow the prompt hook names (TASK FLOW line), or None."""
    for r in search(text, 3, model):
        if r["refused"] or r["score"] < TASK_FLOW_FLOOR or not _prompt_trigger(get(r["id"]) or {}):
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
