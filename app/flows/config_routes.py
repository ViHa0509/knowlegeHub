"""Flow editor routes (KH 7.1): catalogs, expression check, scope-event runs, event fire, flow and project-type
edits. Edits live in kh_flow_def / kh_project_type over the seeds (app/flows/store.py)."""
from __future__ import annotations

import ast
import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request

from app import config
from app import ctx_util as U

log = logging.getLogger(__name__)

_EVENTS = Path(config.ROOT) / "seeds" / "flow_events.json"
_NODES = Path(config.ROOT) / "seeds" / "flow_nodes.json"
_TYPES = Path(config.ROOT) / "seeds" / "project_types.json"
FIRE_EVENT = "kh.daily_0200"
EVERY_PROJECT = "every project"

_ALLOWED = (ast.Expression, ast.Name, ast.Attribute, ast.Call, ast.Constant, ast.Compare, ast.BoolOp, ast.BinOp,
            ast.UnaryOp, ast.Load, ast.keyword, ast.Subscript, ast.Tuple, ast.List, ast.Slice, ast.IfExp,
            ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd, ast.Invert, ast.Add, ast.Sub, ast.Mult, ast.Div,
            ast.FloorDiv, ast.Mod, ast.Pow, ast.BitAnd, ast.BitOr, ast.BitXor, ast.Eq, ast.NotEq, ast.Lt,
            ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn, ast.Is, ast.IsNot)
_NAMES = {"pl", "steps", "input", "prompt", "caller"}


def _json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def check_expr(expr: str, kind: str, upstream: list[str]) -> dict:
    """Check a Polars-style expression without Polars: parse with ast, allow a small node set, list the
    pl.col("...") reads, and for kind 'when' require every read to be given by an upstream step."""
    def bad(msg: str, reads: Optional[list] = None) -> dict:
        return {"ok": False, "reads": reads or [], "error": msg}

    if kind not in ("filter", "when"):
        return bad("kind must be one of filter, when")
    text = (expr or "").strip()
    if not text:
        return bad("empty expression")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as e:
        return bad(f"not a Python/Polars expression: {e.msg} (column {e.offset or 0})")
    reads: list[str] = []
    for n in ast.walk(tree):
        if not isinstance(n, _ALLOWED):
            return bad(f"'{type(n).__name__}' is not allowed in a flow expression")
        if isinstance(n, ast.Name) and n.id not in _NAMES:
            return bad(f"unknown name '{n.id}': use pl, steps[\"<node name>\"], input, prompt or caller")
        if isinstance(n, ast.Attribute) and n.attr.startswith("_"):
            return bad(f"'.{n.attr}' is not allowed in a flow expression")
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "col"
                and isinstance(n.func.value, ast.Name) and n.func.value.id == "pl"):
            for a in n.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value not in reads:
                    reads.append(a.value)
    if kind == "when":
        for r in reads:
            if r not in (upstream or []):
                return bad(f'reads "{r}" which no upstream step gives', reads)
    return {"ok": True, "reads": reads, "error": ""}


def types_merged() -> list[dict]:
    from app.flows import store

    seed = _json(_TYPES, {}).get("types") or []
    ed = store.type_edits()
    out = [ed.pop(str(t.get("id")), t) for t in seed]
    out.extend(ed.values())
    return out


def _audit(action: str, target: str, before: Any, after: Any) -> None:
    try:
        from app import audit

        audit.record(action, EVERY_PROJECT, target, before=before, after=after)
    except Exception:  # noqa: BLE001
        pass


def attach(app: FastAPI) -> None:
    from app import flows as F
    from app.flows import runners, store

    @app.get("/flows/events", tags=["flows"])
    def flows_events():
        return _json(_EVENTS, {"sources": [], "listener": {}})

    @app.get("/flows/nodes", tags=["flows"])
    def flows_nodes():
        return _json(_NODES, {"nodes": [], "blocks": [], "pseudo": [], "ops_by": {}})

    @app.post("/flows/expr/check", tags=["flows"])
    def flows_expr_check(body: Optional[dict] = Body(None)):
        b = body or {}
        ups = [str(x) for x in (b.get("upstream") or []) if isinstance(x, str)]
        return check_expr(str(b.get("expr") or ""), str(b.get("kind") or "when"), ups)

    @app.get("/flows/scope-events/runs", tags=["flows"])
    def scope_event_runs(type: str = Query(...), sig: Optional[str] = None, limit: int = Query(20, ge=1, le=200)):
        spaces: list[str] = []
        try:
            from app.spaces import api as spaces_api

            spaces = [x["id"] for x in spaces_api.list_spaces()["items"] if x.get("type") == type]
        except Exception:  # noqa: BLE001
            spaces = []
        return {"type": type, "spaces": spaces, "runs": [], "every_s": 300, "on": F.flows_on()}

    @app.post("/flows/events/{event_id}/fire", tags=["flows"])
    async def flows_fire(request: Request, event_id: str, flow: Optional[str] = None):
        if event_id != FIRE_EVENT:
            raise HTTPException(status_code=400, detail=f"only {FIRE_EVENT} is fired on demand; the other KH "
                                                         "events fire when KH does them")
        if flow:
            f = store.get(flow)
            if f is None or not f.get("enabled", True):
                raise HTTPException(status_code=404, detail=f"no enabled flow '{flow}' with the trigger "
                                                             f"{FIRE_EVENT}; save the flow first")
            ids = [flow]
        else:
            ids = [f["id"] for f in store.flows() if f.get("enabled", True) and any(
                t.get("type") == "event" and t.get("event") == FIRE_EVENT for t in f.get("triggers") or [])]
        queued: list[str] = []
        reason = "no flow has this trigger"
        run_id = None
        for fid in ids:
            if fid not in runners.RUNNERS:
                reason = "this build has no runner for the flow"
                continue
            out = await asyncio.to_thread(F.run_flow, fid, {}, U.model_of(request), "event", event_id)
            queued.append(fid)
            run_id = run_id or out.get("trace_id")
        res: dict[str, Any] = {"event": event_id, "queued": bool(queued), "flows": queued, "run_id": run_id}
        if not queued:
            res["reason"] = reason
        return res

    def _valid(flow_id: str, body: Optional[dict]) -> dict:
        b = dict(body or {})
        if not isinstance(b.get("name"), str) or not b["name"].strip():
            raise HTTPException(status_code=400, detail="name required")
        if b.get("id") not in (None, flow_id):
            raise HTTPException(status_code=400, detail="id in the body does not match the path")
        return b

    @app.put("/flows/{flow_id}", tags=["flows"])
    def flows_put(flow_id: str, body: Optional[dict] = Body(None)):
        b = _valid(flow_id, body)
        before = store.get(flow_id)
        if before is not None and before.get("kind") == "system":
            b["kind"] = "system"
        b.setdefault("kind", (before or {}).get("kind") or "task")
        b.setdefault("enabled", True)
        saved = store.save_flow(flow_id, b, F._caller())
        _audit("flow.save", flow_id, before, saved)
        out = dict(saved)
        out["warnings"] = []
        out["problems"] = []
        out["rev"] = store.rev()
        return out

    @app.delete("/flows/{flow_id}", tags=["flows"])
    def flows_delete(flow_id: str):
        before = store.get(flow_id)
        if before is not None and before.get("kind") == "system":
            raise HTTPException(status_code=409, detail=f"flow '{flow_id}' is a system flow and cannot be deleted")
        res = store.delete_flow(flow_id, F._caller())
        if res != "missing":
            _audit("flow.delete", flow_id, before, store.get(flow_id))
        return {"id": flow_id, "result": res}

    @app.put("/project-types/{type_id}", tags=["flows"])
    def types_put(type_id: str, body: Optional[dict] = Body(None)):
        b = dict(body or {})
        if not isinstance(b.get("name"), str) or not b["name"].strip():
            raise HTTPException(status_code=400, detail="name required")
        before = next((t for t in types_merged() if str(t.get("id")) == type_id), None)
        saved = store.save_type(type_id, b, F._caller())
        _audit("project_type.save", type_id, before, saved)
        return saved

    @app.delete("/project-types/{type_id}", tags=["flows"])
    def types_delete(type_id: str):
        before = next((t for t in types_merged() if str(t.get("id")) == type_id), None)
        seed_ids = {str(t.get("id")) for t in (_json(_TYPES, {}).get("types") or [])}
        res = store.delete_type(type_id, seed_ids)
        if res != "missing":
            _audit("project_type.delete", type_id, before, None)
        return {"id": type_id, "result": res}
