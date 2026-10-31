"""Tagged kernel (spec §3.8): routes /kernel/*. Route group `kernel`.

Tier permissions: signed out = everything allowed; common: anyone reads, maintainers write; a personal tier: its
owner or a maintainer; a project tier: project.read / project.write. `tier: "personal"` = the caller's own personal
project (a maintainer or a signed-out caller may name another personal tier explicitly).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from app import ctx_config as C
from app import ctx_util as U
from app.kernel import rules as R
from app.kernel import tags as T

log = logging.getLogger(__name__)

KERNEL_PART_MAX = C.KERNEL_PART_MAX
__all__ = ["attach", "start", "render_blocks", "render_part", "guards_for", "pretool_text", "R", "T"]


# ---------------------------------------------------------------------------
# Tier permissions
# ---------------------------------------------------------------------------


def resolve_tier(tier: Optional[str]) -> Optional[str]:
    if tier is None:
        return None
    t = str(tier).strip()
    if t == "personal":
        own = U.personal_tier()
        if own is None:
            raise HTTPException(status_code=400, detail="tier 'personal' needs a signed-in caller; name the tier")
        return own
    return t


def may_read(tier: str) -> bool:
    if not U.signed_in():
        return True
    kind = R.tier_kind(tier)
    if kind == "common":
        return True
    if kind == "personal":
        return tier == U.personal_tier() or U.is_maintainer()
    return U.may(tier, "project.read") or U.may(tier, "project.write")


def may_write(tier: str) -> bool:
    if not U.signed_in():
        return True
    kind = R.tier_kind(tier)
    if kind == "common":
        return U.is_maintainer()
    if kind == "personal":
        return tier == U.personal_tier() or U.is_maintainer()
    return U.may(tier, "project.write")


def _need_read(tier: str) -> None:
    if not may_read(tier):
        raise HTTPException(status_code=403, detail=f"no read access to kernel tier '{tier}'")


def _need_write(tier: str) -> None:
    if not may_write(tier):
        raise HTTPException(status_code=403, detail=f"no write access to kernel tier '{tier}'")


def _personal_param(personal: Optional[str]) -> Optional[str]:
    """The personal tier used for resolve/render: the caller's own; another one only for maintainers/signed out."""
    own = U.personal_tier()
    if personal and personal != "personal" and personal != own:
        if U.is_maintainer():
            return personal
        raise HTTPException(status_code=403, detail="only a maintainer may name another personal tier")
    return own


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def render_blocks(project: Optional[str] = None, personal: Optional[str] = None, legacy: bool = True) -> list[str]:
    from app import hooks, personalization
    from app.policy_actions import lint

    legacy_rows = (personalization.own_rows(personal) or []) if legacy else []
    if R.count(R.COMMON) > 0:
        proj = project if project and U.may(project, "project.read") else None
        return R.render(proj, personal, legacy_rows)
    blocks = [lint.render(hooks.served_text("SessionStart"))]
    if legacy_rows:
        blocks.append(personalization._HEADER + "\n" + "\n".join(f"- {r}" for r in legacy_rows))
    return [b for b in blocks if b]


def render_part(blocks: list[str], part: int, max_chars: int) -> tuple[str, int]:
    ps = R.parts(blocks, max_chars)
    if part <= 0:
        return "\n\n".join(blocks), len(ps)
    return (ps[part - 1] if part - 1 < len(ps) else ""), len(ps)


def guards_for(tool: str, project: Optional[str] = None, personal: Optional[str] = None) -> list[dict]:
    return R.guards(tool, project, personal)


def pretool_text() -> str:
    from app import hooks

    if hooks.enabled_rules("PreToolUseTools"):
        return hooks.served_text("PreToolUseTools")
    return hooks.served_text("PreToolUse")


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


class SuggestBody(BaseModel):
    text: str = Field(..., min_length=2, max_length=4000)
    limit: int = Field(15, ge=1, le=50)


class ProposeBody(BaseModel):
    tag: str
    facet: str
    description: Optional[str] = None
    aliases: Any = None


class ApproveBody(BaseModel):
    tag: str
    status: str = "active"


class DeleteBody(BaseModel):
    id: str


def _refusal(e: R.KernelError) -> JSONResponse:
    return JSONResponse(status_code=e.status, content=e.payload)


def _rule_body(body: dict) -> dict:
    body = dict(body or {})
    tier = resolve_tier(body.get("tier"))
    if tier is None:
        raise R.KernelError(400, "KERNEL-TIER", "tier is required (common, a project id, or personal)")
    body["tier"] = tier
    _need_write(tier)
    return body


def attach(app: FastAPI) -> None:
    T.ensure()
    R.ensure()

    @app.get("/kernel/tags", tags=["kernel"])
    def kernel_tags(facet: Optional[str] = None, status: Optional[str] = "active"):
        return T.list_tags(facet, status or None)

    @app.get("/kernel/tags/suggest", tags=["kernel"])
    def kernel_tags_suggest_get(text: str = Query(..., min_length=2, max_length=4000),
                                limit: int = Query(15, ge=1, le=50)):
        return {"tags": T.suggest(text, limit)}

    @app.post("/kernel/tags/suggest", tags=["kernel"])
    def kernel_tags_suggest_post(body: SuggestBody):
        return {"tags": T.suggest(body.text, body.limit)}

    @app.post("/kernel/tags/propose", tags=["kernel"])
    def kernel_tags_propose(body: ProposeBody):
        tag = (body.tag or "").strip().lower()
        if not T.TAG_RE.match(tag):
            raise HTTPException(status_code=422, detail="bad tag id: use ^[a-z][a-z0-9_]{1,47}$")
        if body.facet not in T.facets():
            raise HTTPException(status_code=422, detail=f"unknown facet '{body.facet}'")
        if not (body.description or "").strip():
            raise HTTPException(status_code=422, detail="description is required")
        if tag in T.all_rows():
            raise HTTPException(status_code=422, detail=f"tag '{tag}' already exists")
        row = T.propose(tag, body.facet, body.description.strip(), body.aliases, U.email())
        similar = T.suggest(f"{tag.replace('_', ' ')} {body.description}", limit=5)
        return {**row, "similar_existing": similar}

    @app.post("/kernel/tags/approve", tags=["kernel"])
    def kernel_tags_approve(body: ApproveBody):
        U.need_maintainer()
        if body.status not in T.STATUSES:
            raise HTTPException(status_code=422, detail="status must be active, proposed or retired")
        if not T.set_status(body.tag, body.status):
            raise HTTPException(status_code=404, detail=f"unknown tag '{body.tag}'")
        return {"tag": body.tag, "status": body.status}

    @app.get("/kernel/rules", tags=["kernel"])
    def kernel_rules(tier: str = "common", all: bool = False):  # noqa: A002
        t = resolve_tier(tier)
        _need_read(t)
        rows = R.list_rules(t, include_disabled=all)
        return {"tier": t, "count": len(rows), "rules": rows}

    @app.post("/kernel/rules/check", tags=["kernel"])
    def kernel_rules_check(body: dict = Body(...)):
        try:
            b = _rule_body(body)
            plan = R.check(b, U.is_maintainer(), U.email())
        except R.KernelError as e:
            return _refusal(e)
        return {"ok": True, "would_store": plan}

    @app.post("/kernel/rules/add", tags=["kernel"])
    def kernel_rules_add(body: dict = Body(...)):
        try:
            b = _rule_body(body)
            return R.add(b, U.is_maintainer(), U.email())
        except R.KernelError as e:
            return _refusal(e)

    @app.post("/kernel/rules/delete", tags=["kernel"])
    def kernel_rules_delete(body: DeleteBody):
        row = R.get_by_id(body.id)
        if row is None:
            return {"deleted": 0}
        _need_write(row["tier"])
        return {"deleted": R.delete(body.id)}

    @app.get("/kernel/resolved", tags=["kernel"])
    def kernel_resolved(project: Optional[str] = None, personal: Optional[str] = None):
        pers = _personal_param(personal)
        proj = project if project and may_read(project) else None
        slots = [{k: v for k, v in e.items() if not k.startswith("_")} for e in R.resolve(proj, pers)]
        return {"count": len(slots), "slots": slots}

    @app.get("/kernel/render", tags=["kernel"], response_class=PlainTextResponse)
    async def kernel_render(project: Optional[str] = None, personal: Optional[str] = None, part: int = 0,
                            max_chars: int = Query(KERNEL_PART_MAX, ge=500, le=200000), legacy: bool = True):
        pers = _personal_param(personal)
        blocks = await asyncio.to_thread(render_blocks, project, pers, legacy)
        text, n = render_part(blocks, part, max_chars)
        return PlainTextResponse(text, headers={"X-KH-Kernel-Parts": str(n)})

    @app.get("/kernel/pretool", tags=["kernel"], response_class=PlainTextResponse)
    def kernel_pretool():
        return PlainTextResponse(pretool_text())

    @app.post("/kernel/seed", tags=["kernel"])
    def kernel_seed():
        U.need_maintainer()
        from app import seeding

        return seeding.seed_kernel(force=True)


def start(app: FastAPI) -> None:  # noqa: ARG001
    if not C.SEED_DEFAULTS:
        return
    try:
        from app import seeding

        r = seeding.seed_all()
        log.info("seeds: %s", r)
    except Exception as e:  # noqa: BLE001
        log.warning("kernel seed failed: %s", e)
