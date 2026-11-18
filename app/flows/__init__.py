"""KH 7.x flows: /flows (list, search, one flow, run, runs, answer), /hooks/{id}/run, /helper/manifest,
/project-types.

Like KH 7.1 the routes are always attached; KH_FLOWS (default on) is what the helper manifest reports as
flows_on. Definitions are seeded from the KH 7.1 build (seeds/flows.seed.json); see app/flows/runners.py
for which ones this build can run.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from app import config
from app import ctx_util as U

log = logging.getLogger(__name__)

_MANIFEST = Path(config.ROOT) / "seeds" / "helper_manifest.json"
_TYPES = Path(config.ROOT) / "seeds" / "project_types.json"


def flows_on() -> bool:
    from app.spaces import switch

    return switch("KH_FLOWS", True)


def _caller() -> str:
    from app import auth

    try:
        p = auth.current()
        return getattr(p, "email", None) or auth.current_actor() or "local"
    except Exception:  # noqa: BLE001
        return "local"


def run_flow(flow_id: str, inp: dict, model, origin: str = "request", event: Optional[str] = None) -> dict:
    from app.flows import runners, store

    f = store.get(flow_id)
    if f is None:
        raise HTTPException(status_code=404, detail=f"no such flow ('{flow_id}')")
    if not f.get("enabled", True):
        raise HTTPException(status_code=409, detail=f"flow '{flow_id}' is disabled")
    run_id = store.new_run_id()
    t0 = store.now_ms()
    fn = runners.RUNNERS.get(flow_id)
    if fn is None:
        result = runners.not_runnable(flow_id)
    else:
        try:
            result = fn(inp or {}, model)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("flow %s failed", flow_id)
            result = {"status": "failed", "outcome": "failed", "message": f"{type(e).__name__}: {e}", "text": "",
                      "trace": []}
    ms = int(store.now_ms() - t0)
    out: dict[str, Any] = {"flow": flow_id, "version": int(f.get("version") or 1)}
    out.update(result)
    text = out.get("text") or ""
    part = int(((f.get("answer") or {}).get("part")) or 9000)
    if "parts" not in out:
        out["parts"] = [text[i:i + part] for i in range(0, len(text), part)] or [""]
    out["trace_id"] = run_id
    out["rev"] = store.rev()
    store.record(run_id, f, inp or {}, out, ms, _caller(), origin, event)
    return out


def manifest() -> dict:
    from app.flows import store

    try:
        m = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        m = {"tools": [], "hooks": {}, "context": []}
    m["flows_on"] = flows_on()
    m.setdefault("tool_flows", {})
    m["rev"] = store.rev()
    return m


class RunBody(BaseModel):
    input: Optional[dict] = None


class AnswerBody(BaseModel):
    answer: Any = None


def attach(app: FastAPI) -> None:
    from app.flows import runners, store

    @app.get("/helper/manifest", tags=["flows"])
    def helper_manifest():
        return manifest()

    @app.get("/project-types", tags=["flows"])
    def project_types():
        try:
            return json.loads(_TYPES.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"types": []}

    @app.get("/project-types/{type_id}", tags=["flows"])
    def project_type(type_id: str):
        try:
            types = json.loads(_TYPES.read_text(encoding="utf-8")).get("types") or []
        except (OSError, ValueError):
            types = []
        for t in types:
            if str(t.get("name", "")).lower() == type_id.lower() or str(t.get("id", "")) == type_id:
                return t
        raise HTTPException(status_code=404, detail=f"no such project type ('{type_id}')")

    @app.get("/flows", tags=["flows"])
    async def flows_list(request: Request, q: Optional[str] = None, k: int = Query(5, ge=1, le=50)):
        if q and q.strip():
            rows = await asyncio.to_thread(store.search, q, k, U.model_of(request))
            return {"query": q, "flows": [{kk: v for kk, v in r.items() if kk != "refused"} for r in rows]}
        return {"rev": store.rev(), "flows": [store.listing(f) for f in store.flows()]}

    @app.get("/flows/runs", tags=["flows"])
    def flows_runs(limit: int = Query(50, ge=1, le=500), flow: Optional[str] = None):
        return {"runs": store.runs(limit, flow)}

    @app.get("/flows/runs/{run_id}", tags=["flows"])
    def flows_run(run_id: str):
        r = store.run(run_id)
        if r is None:
            raise HTTPException(status_code=404, detail=f"no such run ('{run_id}')")
        return r

    @app.post("/flows/runs/{run_id}/answer", tags=["flows"])
    def flows_answer(run_id: str, body: AnswerBody):
        r = store.run(run_id)
        if r is None:
            raise HTTPException(status_code=404, detail=f"no such run ('{run_id}')")
        raise HTTPException(status_code=409, detail=f"run '{run_id}' is not waiting for an answer "
                                                    f"(status {r['status']})")

    @app.get("/flows/match", tags=["flows"])
    async def flows_match(request: Request, text: str):
        return {"text": text, "flow": await asyncio.to_thread(store.nearest_task_flow, text, U.model_of(request))}

    @app.get("/flows/{flow_id}", tags=["flows"])
    def flows_one(flow_id: str):
        f = store.get(flow_id)
        if f is None:
            raise HTTPException(status_code=404, detail=f"no such flow ('{flow_id}')")
        out = dict(f)
        out["runnable"] = flow_id in runners.RUNNERS
        return out

    @app.post("/flows/{flow_id}/run", tags=["flows"])
    async def flows_run_one(request: Request, flow_id: str, body: Optional[RunBody] = None):
        inp = (body.input if body else None) or {}
        return await asyncio.to_thread(run_flow, flow_id, inp, U.model_of(request))

    @app.post("/hooks/{hook_id}/run", tags=["flows"])
    async def hooks_run(request: Request, hook_id: str, body: Optional[RunBody] = None):
        fid = runners.HOOK_FLOWS.get(hook_id)
        if fid is None:
            raise HTTPException(status_code=404, detail=f"no hook flow for '{hook_id}'")
        inp = (body.input if body else None) or {}
        out = await asyncio.to_thread(run_flow, fid, inp, U.model_of(request), "hook", f"hook.{hook_id}")
        out.pop("items", None)
        out.pop("output", None)
        if hook_id != "SessionStart":
            out.pop("parts", None)
        return out

    log.info("Flow routes attached (/project-types, /flows, /helper/manifest); KH_FLOWS=%s",
             "on" if flows_on() else "off")
