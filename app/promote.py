"""Promote: personal rules -> common tier (spec §4.5, kernel rule 10).

Nightly, a model looks for rules several distinct owners ask for and proposes a new text for one step of one
common action; a maintainer approves via `/distill/approve?project=orchestrator`, which writes the rule into the
every-project policy tier. The kernel itself is never changed by a proposal.

Data read here (owned by other areas, accessed without importing their modules):
- personalization rows: vector collections `kh_personalization_<personal id>` (scope personalization);
- project feedback: knowledge rows with `kind=feedback` in `kh_know_<project>`;
- common actions/steps: SQL on `policy_actions` / `policy_rules` (tier "every project", via app.distill);
- kernel: SessionStart text of the `hook_rules` table (SQL, read only).
"""
from __future__ import annotations

import hashlib
import json
import logging
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import config, distill, sqldb
from app import vector_store as vs
from app.hotlog import parse_ts

log = logging.getLogger(__name__)

TEXT_MAX = 600
ROW_TEXT_MAX = 1500


def enabled() -> bool:
    return (config._env("KH_PROMOTE", "off") or "off").lower() in ("on", "true", "1")


def min_people() -> int:
    return config._int("KH_PROMOTE_MIN_PEOPLE", 2)


def max_rows() -> int:
    return config._int("KH_PROMOTE_MAX_ROWS", 400)


def _check_on() -> None:
    if not enabled():
        raise HTTPException(status_code=403, detail="promote is off")


def owner_of(project: str) -> str:
    if vs.is_personal(project):
        return "person-" + hashlib.sha256(project.encode()).hexdigest()[:8]
    return f"project:{project}"


def rows() -> list[dict]:
    """Every personal project's personalization rows + every other project's kind=feedback knowledge rows;
    status active, not guard_tool, newest first, max KH_PROMOTE_MAX_ROWS."""
    out = []
    pers_prefix = vs.scope_prefix(config.PERSONAL_SCOPE)
    know_prefix = vs.scope_prefix("knowledge")
    for name in vs.list_collection_names():
        if name.startswith(pers_prefix):
            project, layer, where = name[len(pers_prefix):], "personal", None
            if not vs.is_personal(project):
                continue
        elif name.startswith(know_prefix):
            project, layer = name[len(know_prefix):], "project"
            if vs.is_personal(project) or project == config.ORCHESTRATOR_ID:
                continue
            where = {"kind": {"$eq": "feedback"}}
        else:
            continue
        h = vs.get_existing(name)
        if h is None:
            continue
        for r in vs.get_vt(h, where=where):
            m = r["metadata"] or {}
            if (m.get("status") or "active") != "active" or m.get("guard_tool"):
                continue
            out.append({"id": r["vector_id"], "owner": owner_of(project), "layer": layer,
                        "text": (r.get("source_text") or "")[:ROW_TEXT_MAX], "ts": m.get("ts") or m.get("created_at")})
    out.sort(key=lambda x: parse_ts(x["ts"]), reverse=True)
    return out[:max_rows()]


def kernel_text() -> str:
    try:
        with sqldb.connect() as c:
            if not sqldb.table_exists(c, "hook_rules"):
                return ""
            rs = c.execute("SELECT content FROM hook_rules WHERE hook_type=? AND enabled=1 ORDER BY position, id",
                           ("SessionStart",)).fetchall()
            return "\n\n".join(r[0] or "" for r in rs)
    except Exception as e:  # noqa: BLE001
        log.debug("kernel text unavailable: %s", e)
        return ""


def proposals() -> list[dict]:
    items = distill.items_of(config.ORCHESTRATOR_ID, distill.OPEN)
    out = []
    for it in items:
        m = it["metadata"]
        if m.get("tier") != "common":
            continue
        out.append({"id": it["id"], "title": it["title"], "text": it["text"], "status": it["status"],
                    "target_action": m.get("target_action"), "target_sub_action": m.get("target_sub_action"),
                    "people": m.get("people"), "sources": it["sources"]})
    return out


def next_payload() -> dict:
    _check_on()
    return {"rows": rows(), "common": distill.common_actions(), "kernel": kernel_text(), "proposals": proposals(),
            "rules": {"min_people": min_people(), "text_max": TEXT_MAX, "tier": "common"}}


class PromoteItem(BaseModel):
    title: str = ""
    text: str = ""
    target_action: str = ""
    target_sub_action: str = ""
    sources: list[str] = Field(default_factory=list)
    why: str = ""


class PromoteSubmit(BaseModel):
    items: list[dict] = Field(default_factory=list)
    distilled_by: str = ""
    dry_run: bool = False


def submit(body: PromoteSubmit | dict, caller=None, model=None) -> dict:
    _check_on()
    b = body if isinstance(body, PromoteSubmit) else PromoteSubmit(**body)
    forced_dry = distill.is_worker(caller) and distill.worker_mode() == "dry"
    dry = bool(b.dry_run) or forced_dry
    current = {r["id"]: r for r in rows()}
    common = {a["action"]: a for a in distill.common_actions()}
    orch = config.ORCHESTRATOR_ID
    by = b.distilled_by or distill.caller_name(caller)
    results = []
    for n, raw in enumerate(b.items or []):
        def rej(reason, n=n):
            return {"n": n, "result": "rejected", "reason": reason}

        try:
            it = PromoteItem(**{k: v for k, v in (raw or {}).items() if k in PromoteItem.model_fields})
        except Exception as e:  # noqa: BLE001
            results.append(rej(f"invalid item: {e}"))
            continue
        title, text = it.title.strip(), it.text.strip()
        if not (1 <= len(title) <= distill.title_chars()):
            results.append(rej(f"title must be 1..{distill.title_chars()} characters"))
            continue
        if not (1 <= len(text) <= TEXT_MAX):
            results.append(rej(f"text must be 1..{TEXT_MAX} characters"))
            continue
        if it.target_action not in common:
            results.append(rej(f"target_action '{it.target_action}' is not a common action"))
            continue
        if not it.target_sub_action.strip():
            results.append(rej("target_sub_action must not be empty"))
            continue
        if not it.sources or any(s not in current for s in it.sources):
            results.append(rej("sources must all be ids of the current rows"))
            continue
        owners = {current[s]["owner"] for s in it.sources}
        if len(owners) < min_people():
            results.append(rej(f"needs at least {min_people()} distinct owners (got {len(owners)})"))
            continue
        lk = distill.leak(f"{title}\n{text}", orch, strict=True, share=True)
        if lk:
            results.append(rej(f"leak gate: '{lk}'"))
            continue
        vec = distill.encode(f"{title}. {text}", model)
        near, score = distill._nearest(orch, vec, ["policy_proposal"], distill.OPEN)
        if near is not None and score >= distill.same_floor() and near["metadata"].get("tier") == "common":
            if not dry:
                srcs = distill._merge_sources(near["sources"], it.sources)
                distill._update_meta(orch, near["id"], {"sources_json": json.dumps(srcs),
                                                        "last_confirmed": distill._now(),
                                                        "evidence_count": max(near["evidence_count"], len(srcs))})
            results.append({"n": n, "result": "merged", "id": near["id"]})
            continue
        tsa = it.target_sub_action.strip()
        key = distill.dkey(orch, "policy_proposal", it.target_action, tsa, "common")
        new_id = distill._new_id(orch, key)
        if not dry:
            seq = distill.step_seq(it.target_action, tsa)
            now = distill._now()
            meta = {"kind": "policy_proposal", "action": "-", "context": "-", "entities_json": "[]",
                    "entity_key": "-", "dkey": key, "status": "proposed", "tier": "common", "people": len(owners),
                    "seq": seq if seq is not None else 99, "target_action": it.target_action,
                    "target_sub_action": tsa, "sources_json": json.dumps(it.sources[-50:]),
                    "evidence_count": len(it.sources), "first_seen": now, "last_confirmed": now,
                    "distilled_by": by, "distilled_at": now, "why": it.why[:500]}
            distill._write(orch, new_id, meta, title, text, model, vec)
        results.append({"n": n, "result": "accepted", "id": new_id})
    out = {"dry_run": dry, "results": results}
    if forced_dry:
        out["forced_dry"] = True
    return out


def attach(app: FastAPI) -> None:
    @app.get("/promote/next", tags=["promote"])
    def promote_next():
        return next_payload()

    @app.post("/promote/submit", tags=["promote"])
    def promote_submit(body: PromoteSubmit):
        return submit(body, distill._principal(), getattr(app.state, "model", None))

