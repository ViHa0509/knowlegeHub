"""Policy action table (spec §3.7.2): routes /policy/actions*, /policy/plan, /policy/rule*, /policy/action/*,
/policy/lint. Route group `policy_actions`.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app import ctx_util as U
from app.policy_actions import lint, store
from app.policy_actions.store import EVERY

log = logging.getLogger(__name__)

__all__ = ["attach", "store", "lint", "EVERY", "canon", "menu_for_turn"]


def canon(project: Optional[str]) -> Optional[str]:
    """A string that already owns rows is kept; else projects.canonical_or_none; else verbatim (`_canon`)."""
    if project is None:
        return None
    p = str(project).strip()
    if not p:
        return None
    if p == EVERY or store.owns_rows(p):
        return p
    try:
        from app import projects

        fn = getattr(projects, "canonical_or_none", None)
        if callable(fn):
            c = fn(p)
            if c:
                return c
    except Exception as e:  # noqa: BLE001
        log.debug("canonical_or_none failed: %s", e)
    return p


def _tier(project: Optional[str]) -> str:
    return canon(project) or EVERY


def menu_for_turn(project: Optional[str]) -> list[dict]:
    """`policy_store.menu(best)` shaped for the turn context: [{action, project, when}]."""
    from app import ctx_config as C

    return [{"action": a["action"], "project": a["project"], "when": U.short(a.get("description"), C.TURN_WHEN_CHARS)}
            for a in store.list_actions(project)]


class ActionSet(BaseModel):
    action: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1)
    project: Optional[str] = None
    source_policy: Optional[str] = ""
    priority: int = 100
    allow_oversize: bool = False
    reason: Optional[str] = None


class RuleSet(BaseModel):
    action: str = Field(..., min_length=1, max_length=200)
    sub_action: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1)
    project: Optional[str] = None
    seq: int = 0
    clauses: Optional[str] = ""
    allow_oversize: bool = False
    reason: Optional[str] = None


class RuleDelete(BaseModel):
    action: str
    sub_action: str
    project: Optional[str] = None


class ActionDelete(BaseModel):
    action: str
    project: Optional[str] = None
    cascade: bool = False


class Bulk(BaseModel):
    actions: list[dict[str, Any]] = Field(default_factory=list)
    rules: list[dict[str, Any]] = Field(default_factory=list)
    replace: bool = False


def _read_check(project: Optional[str]) -> None:
    if project and project != EVERY and not U.is_maintainer():
        U.need(project, "project.read")


def _write_check(tier: str) -> None:
    if tier == EVERY:
        if not (U.is_maintainer() or U.may("orchestrator", "project.write")):
            raise HTTPException(status_code=403, detail="missing permission project.write on this project")
        return
    U.need(tier, "project.write")


def _actions_payload(project: Optional[str]) -> dict:
    tier = canon(project)
    _read_check(tier)
    acts = store.menu(tier)
    return {"count": len(acts), "project": tier or EVERY, "requested_project": project, "actions": acts,
            "table": store.counts()}


def _unknown(action: str, project: Optional[str]) -> JSONResponse:
    return JSONResponse(status_code=404, content={
        "error": "UNKNOWN-ACTION", "action": action, "project": project or EVERY,
        "known_actions": [a["action"] for a in store.list_actions(project)],
        "hint": "pick a label from GET /policy/actions, or answer none - never guess"})


def attach(app: FastAPI) -> None:
    store.ensure()
    try:
        from app import projects

        projects.on_delete(store.delete_project_rows)
    except Exception as e:  # noqa: BLE001
        log.warning("policy_actions on_delete hook not registered: %s", e)

    @app.get("/policy/actions", tags=["policy"])
    async def policy_actions(project: Optional[str] = None):
        return await asyncio.to_thread(_actions_payload, project)

    @app.get("/policy/actions/raw", tags=["policy"], response_class=PlainTextResponse)
    async def policy_actions_raw(project: Optional[str] = None):
        tier = canon(project)
        _read_check(tier)
        return PlainTextResponse(await asyncio.to_thread(store.menu_text, tier))

    @app.get("/policy/actions/projects", tags=["policy"])
    def policy_actions_projects():
        out = []
        for p in store.owner_projects():
            if p == EVERY or U.may(p, "project.read"):
                out.append(p)
        if EVERY not in out:
            out.insert(0, EVERY)
        return {"count": len(out), "projects": out}

    @app.get("/policy/plan", tags=["policy"])
    def policy_plan(action: str, project: Optional[str] = None):
        tier = canon(project)
        _read_check(tier)
        rules = store.plan(action, tier)
        if not rules and not store.has_action(action, tier):
            return _unknown(action, tier)
        store.record_fetch(tier or EVERY, action)
        return {"action": action, "project": tier or EVERY, "requested_project": project, "count": len(rules),
                "rules": rules}

    @app.get("/policy/rule", tags=["policy"])
    def policy_rule(action: str, sub_action: str, project: Optional[str] = None):
        tier = canon(project)
        _read_check(tier)
        r = store.get_rule(action, sub_action, tier)
        if r is None:
            return JSONResponse(status_code=404, content={
                "error": "NO-RULE", "action": action, "sub_action": sub_action, "project": tier or EVERY,
                "sub_actions_of_action": [x["sub_action"] for x in store.plan(action, tier)]})
        return r

    @app.post("/policy/action/set", tags=["policy"])
    def policy_action_set(body: ActionSet):
        tier = _tier(body.project)
        _write_check(tier)
        errors, warnings = lint.check_action(body.action, body.description, tier)
        refusal = lint.gate(errors, warnings, body.model_dump())
        if refusal is not None:
            return refusal
        row = store.set_action(tier, body.action, body.description, body.source_policy or "", body.priority)
        return {**row, "warnings": warnings}

    @app.post("/policy/rule/set", tags=["policy"])
    def policy_rule_set(body: RuleSet):
        tier = _tier(body.project)
        _write_check(tier)
        if store.get_action(body.action, tier) is None and store.get_action(body.action, EVERY) is None:
            return _unknown(body.action, tier)
        errors, warnings = lint.check_rule(body.action, body.sub_action, body.content, tier)
        refusal = lint.gate(errors, warnings, body.model_dump())
        if refusal is not None:
            return refusal
        row = store.set_rule(tier, body.action, body.sub_action, body.content, body.seq, body.clauses or "")
        return {**row, "warnings": warnings}

    @app.post("/policy/rule/delete", tags=["policy"])
    def policy_rule_delete(body: RuleDelete):
        tier = _tier(body.project)
        _write_check(tier)
        return {"deleted": store.delete_rule(tier, body.action, body.sub_action)}

    @app.post("/policy/action/delete", tags=["policy"])
    def policy_action_delete(body: ActionDelete):
        tier = _tier(body.project)
        _write_check(tier)
        rules = store.tier_rules(body.action, tier)
        if rules and not body.cascade:
            return JSONResponse(status_code=409, content={
                "error": "ACTION-HAS-RULES", "action": body.action, "project": tier,
                "sub_actions": [r["sub_action"] for r in rules],
                "hint": "delete the rules first, or resend with cascade=true"})
        return store.delete_action(tier, body.action, body.cascade)

    @app.post("/policy/actions/bulk", tags=["policy"])
    def policy_actions_bulk(body: Bulk):
        U.need_maintainer()
        for a in body.actions:
            if not a.get("action"):
                raise HTTPException(status_code=422, detail="every action needs 'action'")
        declared = {((a.get("project") or EVERY), a["action"]) for a in body.actions}
        if not body.replace:
            declared |= {(a["project"], a["action"]) for a in store.all_actions()}
        bad = [{"project": r.get("project") or EVERY, "action": r.get("action"), "sub_action": r.get("sub_action")}
               for r in body.rules
               if ((r.get("project") or EVERY), r.get("action")) not in declared or not r.get("sub_action")]
        if bad:
            return JSONResponse(status_code=400, content={
                "error": "UNDECLARED-ACTION", "rules": bad[:20],
                "hint": "declare every (project, action) in 'actions' (or the table) before its rules"})
        return store.bulk(body.actions, body.rules, body.replace)

    @app.get("/policy/lint", tags=["policy"])
    def policy_lint():
        U.need_maintainer()
        return lint.report()


def start(app: FastAPI) -> None:  # noqa: ARG001
    from app import ctx_config as C

    if not C.SEED_DEFAULTS:
        return
    try:
        from app.seeding import seed_policy_actions

        seed_policy_actions()
    except Exception as e:  # noqa: BLE001
        log.warning("policy action seed failed: %s", e)
