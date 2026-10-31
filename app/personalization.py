"""Personalization render and tool guards (spec §3.9, §3.8.5). Route group `personalization`.

    GET /personalization/render?k=40&seed=    text/plain: _HEADER + "- row" lines (16000-char cap)
    GET /personalization/guards?tool=&project=  {tool, guards:[{id, text, decision, when, tier?, slot?}]}
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from app import config
from app import ctx_config as C
from app import ctx_util as U

log = logging.getLogger(__name__)

_HEADER = ("KnowledgeHub PERSONALIZATION auto-loaded from KH scope=personalization (EDIT these preferences in KH, NOT "
           "in settings.json). FOLLOW them this session, especially any communication-style / verbosity rule (reply "
           "short, keyword-driven, no recap). This is the PERSONAL LAYER (kernel rule 10): on any overlap or conflict "
           "it wins over the kernel and the shared policies, except rule (0), a 403 and never typing a secret. "
           "Preferences:")
_MAX_CHARS = C.PERSONALIZATION_MAX_CHARS
SEED_TEXT = ("fen user preference rule communication style answer format verbosity tone design philosophy working "
             "directory tools scalability estimate")
EMPTY = "\n(no personalization entries found)"


def _active(meta: dict | None) -> bool:
    return str((meta or {}).get("status") or "active") == "active"


def own_rows(personal: Optional[str] = None) -> Optional[list[str]]:
    """Every active row of the caller's own personalization collection, oldest first. None when signed out."""
    from app import vector_store as vs

    pid = personal or U.personal_tier()
    if not pid:
        return None
    h = vs.get_existing(vs.part_collection_name(config.PERSONAL_SCOPE, pid))
    if h is None:
        return []
    rows = [r for r in vs.get_vt(h) if _active(r.get("metadata"))]

    def key(r):
        m = r.get("metadata") or {}
        return str(m.get("ts") or m.get("created_at") or "")

    rows.sort(key=key)
    return [U.collapse(r.get("source_text")) for r in rows if U.collapse(r.get("source_text"))]


def _by_meaning(seed: str, k: int, model=None) -> list[str]:
    from app.retriever import query_quick_knowledge

    pid = U.personal_tier()
    hits = query_quick_knowledge(seed, k=max(1, k), model=model, scope=config.PERSONAL_SCOPE, project=pid)
    out, total = [], 0
    for h in hits:
        if not _active(h.get("metadata")):
            continue
        line = U.collapse(h.get("source_text"))
        if not line:
            continue
        if total + len(line) + 3 > _MAX_CHARS:
            break
        total += len(line) + 3
        out.append(line)
    return out


def render_text(k: int = 40, seed: Optional[str] = None, model=None) -> str:
    p = U.principal()
    if p is not None and not getattr(p, "ns", None):
        try:
            from app import personal

            personal.touch(p.email)
        except Exception as e:  # noqa: BLE001
            log.warning("personal.touch failed: %s", e)
    lines = None if seed else own_rows()
    if lines is None or sum(len(x) + 3 for x in lines) > _MAX_CHARS:
        lines = _by_meaning(seed or SEED_TEXT, k if lines is None else len(lines), model)
    if not lines:
        return _HEADER + EMPTY
    return _HEADER + "\n" + "\n".join(f"- {x}" for x in lines)


def _bound_project() -> Optional[str]:
    p = U.principal()
    actor = None
    try:
        from app import auth

        actor = auth.current_actor()
    except Exception:  # noqa: BLE001
        pass
    if p is None or not actor:
        return None
    try:
        from app import session_project

        b = session_project.get(U.personal_tier(), actor)
        if isinstance(b, dict):
            return b.get("project")
    except Exception:  # noqa: BLE001
        return None
    return None


def legacy_guards(tool: str, personal: Optional[str] = None) -> list[dict]:
    from app import vector_store as vs

    pid = personal or U.personal_tier()
    if not pid:
        return []
    h = vs.get_existing(vs.part_collection_name(config.PERSONAL_SCOPE, pid))
    if h is None:
        return []
    out = []
    for r in vs.get_vt(h, where={"guard_tool": {"$eq": tool}}):
        m = r.get("metadata") or {}
        if not _active(m):
            continue
        dec = str(m.get("guard_decision") or "ask").lower()
        when = U.jload(m.get("guard_when"), {})
        out.append({"id": r["vector_id"], "text": U.collapse(r.get("source_text")),
                    "decision": dec if dec in ("ask", "deny") else "ask", "when": when if isinstance(when, dict) else {}})
    return out


def guards(tool: str, project: Optional[str] = None, personal: Optional[str] = None) -> list[dict]:
    from app.kernel import rules as R

    personal = personal or U.personal_tier()
    proj = project or _bound_project()
    if proj and not U.may(proj, "project.read"):
        proj = None
    out: list[dict] = []
    try:
        out.extend(R.guards(tool, proj, personal))
    except Exception as e:  # noqa: BLE001
        log.warning("kernel guards failed: %s", e)
    try:
        out.extend(legacy_guards(tool, personal))
    except Exception as e:  # noqa: BLE001
        log.warning("legacy guards failed: %s", e)
    return out


def attach(app: FastAPI) -> None:
    @app.get("/personalization/render", tags=["kernel"], response_class=PlainTextResponse)
    async def personalization_render(request: Request, k: int = 40, seed: Optional[str] = None):
        model = U.model_of(request)
        return PlainTextResponse(await asyncio.to_thread(render_text, k, seed, model))

    @app.get("/personalization/guards", tags=["kernel"])
    def personalization_guards(tool: str, project: Optional[str] = None, personal: Optional[str] = None):
        if personal and not (U.is_maintainer() or personal == U.personal_tier()):
            personal = None
        return {"tool": tool, "guards": guards(tool, project, personal)}
