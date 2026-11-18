"""Score thresholds per embedding model (KH 7.1 `/thresholds`).

KH 7.1 keeps each threshold per embedding model: a value stored in the database (set at runtime by a
maintainer) wins, else the code value for that model. The bge-m3 code values below are the ones the
KH 7.1.0 build answers; the bge-small-en-v1.5 values are the v6.5 calibration this rebuild already used.
An environment variable of the same name still wins over both (source "env"), so deployments and tests
that pin a value keep it.

Resolved values are written into the module attributes the code reads at call time (ctx_config,
spaces.mapper, flows.store, flows.runners), at startup and after every change.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import config, sqldb

CODE: dict[str, dict[str, float]] = {
    "bge-m3": {
        "KH_STEP_FLOOR": 0.56, "KH_STEP_KNOW_ENOUGH": 0.64, "KH_TURN_LESSON_FLOOR": 0.48,
        "KH_TURN_DISTILLED_FLOOR": 0.64, "KH_DISTILL_SAME": 0.89, "KH_DISTILL_CONFIRM": 0.78,
        "KH_FLOW_MATCH_FLOOR": 0.66, "KH_LEARNED_ORIGIN_FLOOR": 0.7, "KH_MAP_K": 10.0, "KH_MAP_FLOOR": 0.45,
        "KH_MAP_BUDGET": 5000.0, "KH_MAP_MISSING_FLOOR": 0.58, "KH_HOOK_PACK_CHARS": 9800.0,
    },
    "bge-small-en-v1.5": {
        "KH_STEP_FLOOR": 0.68, "KH_STEP_KNOW_ENOUGH": 0.75, "KH_TURN_LESSON_FLOOR": 0.6,
        "KH_TURN_DISTILLED_FLOOR": 0.75, "KH_DISTILL_SAME": 0.95, "KH_DISTILL_CONFIRM": 0.85,
        "KH_FLOW_MATCH_FLOOR": 0.75, "KH_LEARNED_ORIGIN_FLOOR": 0.78, "KH_MAP_K": 10.0, "KH_MAP_FLOOR": 0.55,
        "KH_MAP_BUDGET": 5000.0, "KH_MAP_MISSING_FLOOR": 0.65, "KH_HOOK_PACK_CHARS": 9800.0,
    },
}
NAMES = list(CODE["bge-m3"])
COUNTS = {"KH_MAP_K", "KH_MAP_BUDGET", "KH_HOOK_PACK_CHARS"}

# name -> [(module, attribute, cast)] that read the value at call time.
TARGETS: dict[str, list[tuple[str, str, type]]] = {
    "KH_STEP_FLOOR": [("app.ctx_config", "STEP_FLOOR", float)],
    "KH_STEP_KNOW_ENOUGH": [("app.ctx_config", "STEP_KNOW_ENOUGH", float)],
    "KH_TURN_LESSON_FLOOR": [("app.ctx_config", "TURN_LESSON_FLOOR", float)],
    "KH_TURN_DISTILLED_FLOOR": [("app.ctx_config", "TURN_DISTILLED_FLOOR", float)],
    "KH_FLOW_MATCH_FLOOR": [("app.flows.store", "FLOW_MATCH_FLOOR", float)],
    "KH_MAP_K": [("app.spaces.mapper", "MAP_K", int)],
    "KH_MAP_FLOOR": [("app.spaces.mapper", "MAP_FLOOR", float)],
    "KH_MAP_BUDGET": [("app.spaces.mapper", "MAP_BUDGET", int)],
    "KH_MAP_MISSING_FLOOR": [("app.spaces.mapper", "MAP_MISSING_FLOOR", float)],
    "KH_HOOK_PACK_CHARS": [("app.flows.runners", "HOOK_PACK_CHARS", int)],
}

_DDL_SQLITE = ("CREATE TABLE IF NOT EXISTS kh_threshold (model TEXT NOT NULL, name TEXT NOT NULL, value REAL, "
               "updated_by TEXT, updated_at TEXT, PRIMARY KEY (model, name))")
_DDL_HANA = ("CREATE COLUMN TABLE kh_threshold (model NVARCHAR(200) NOT NULL, name NVARCHAR(100) NOT NULL, "
             "value DOUBLE, updated_by NVARCHAR(320), updated_at NVARCHAR(32), PRIMARY KEY (model, name))")


def model_name() -> str:
    return (config.EMBEDDING_MODEL or "").strip() or "bge-small-en-v1.5"


def _code(model: str) -> dict[str, float]:
    return CODE.get(model) or CODE["bge-small-en-v1.5"]


def _stored(model: str) -> dict[str, float]:
    try:
        with sqldb.connect() as c:
            sqldb.create_table(c, "kh_threshold", _DDL_SQLITE, _DDL_HANA)
            rows = c.execute("SELECT name, value FROM kh_threshold WHERE model = ?", (model,)).fetchall()
        return {r["name"]: float(r["value"]) for r in rows if r["value"] is not None}
    except Exception:  # noqa: BLE001
        return {}


def _env(name: str) -> Optional[float]:
    v = os.environ.get(name)
    if v is None or not v.strip():
        return None
    try:
        return float(v.strip().strip("'\""))
    except ValueError:
        return None


def resolve(model: Optional[str] = None) -> list[dict[str, Any]]:
    model = model or model_name()
    code, stored = _code(model), _stored(model)
    out = []
    for n in NAMES:
        env = _env(n)
        if env is not None:
            val, src = env, "env"
        elif n in stored:
            val, src = stored[n], "stored"
        else:
            val, src = code[n], "code"
        out.append({"name": n, "value": val, "stored": stored.get(n), "code": code[n], "source": src})
    return out


def value(name: str) -> float:
    for t in resolve():
        if t["name"] == name:
            return float(t["value"])
    raise KeyError(name)


def apply() -> dict[str, float]:
    import importlib

    done = {}
    for t in resolve():
        for mod, attr, cast in TARGETS.get(t["name"], []):
            try:
                m = importlib.import_module(mod)
            except Exception:  # noqa: BLE001
                continue
            setattr(m, attr, cast(t["value"]))
        done[t["name"]] = t["value"]
    return done


def _check(name: str, val: Any) -> float:
    if name not in NAMES:
        raise HTTPException(status_code=404, detail=f"unknown threshold {name}; known: {', '.join(NAMES)}")
    try:
        v = float(val)
    except (TypeError, ValueError):
        v = None
    if name in COUNTS:
        if v is None or v < 1:
            raise HTTPException(status_code=422, detail="value must be a number >= 1")
    elif v is None or not 0 <= v <= 1:
        raise HTTPException(status_code=422, detail="value must be a number in [0, 1]")
    return v


def set_value(name: str, val: Any, model: Optional[str] = None, who: str = "") -> dict:
    v = _check(name, val)
    model = model or model_name()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with sqldb.connect() as c:
        sqldb.create_table(c, "kh_threshold", _DDL_SQLITE, _DDL_HANA)
        sqldb.upsert(c, "kh_threshold", ["model", "name"],
                     {"model": model, "name": name, "value": v, "updated_by": who, "updated_at": now})
    apply()
    return {"model": model, "name": name, "value": v, "stored": v}


def clear(name: str, model: Optional[str] = None) -> dict:
    if name not in NAMES:
        raise HTTPException(status_code=404, detail=f"unknown threshold {name}; known: {', '.join(NAMES)}")
    model = model or model_name()
    with sqldb.connect() as c:
        sqldb.create_table(c, "kh_threshold", _DDL_SQLITE, _DDL_HANA)
        n = c.execute("DELETE FROM kh_threshold WHERE model = ? AND name = ?", (model, name)).rowcount
    apply()
    return {"name": name, "cleared": bool(n and n > 0), "value": value(name)}


class SetBody(BaseModel):
    name: str = ""
    value: Any = None
    model: Optional[str] = None


def attach(app: FastAPI) -> None:
    from app import ctx_util as U

    apply()

    @app.get("/thresholds", tags=["thresholds"])
    def thresholds_get(model: Optional[str] = None):
        m = model if model in CODE else model_name()
        return {"model": m, "thresholds": resolve(m)}

    @app.put("/thresholds", tags=["thresholds"])
    def thresholds_put(body: SetBody):
        if body.name not in NAMES:
            raise HTTPException(status_code=404, detail=f"unknown threshold {body.name}; known: {', '.join(NAMES)}")
        _check(body.name, body.value)
        U.need_maintainer()
        who = ""
        try:
            from app import auth

            p = auth.current()
            who = getattr(p, "email", "") or ""
        except Exception:  # noqa: BLE001
            pass
        return set_value(body.name, body.value, body.model if body.model in CODE else None, who)

    @app.delete("/thresholds/{name}", tags=["thresholds"])
    def thresholds_delete(name: str, model: Optional[str] = None):
        U.need_maintainer()
        return clear(name, model if model in CODE else None)
