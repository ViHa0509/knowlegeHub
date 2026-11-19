"""KH 7.1 home admin routes: embedding (read only), review of proposed lessons, log retention."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app import config
from app import vector_store as vs

EMBEDDING_501 = ("embedding admin is not in this build: start KH with KH_EMBEDDING_MODEL=<model> "
                 "and its own KH_HOOKS_DB")
RETENTION_OFF_NOTE = "log retention is off (KH_LOG_RETENTION_DAYS=0): logs are kept forever"
OPEN_STATUSES = ("proposed", "contested")


def _audit(action: str, project: str, target: str, before=None, after=None, **extra) -> None:
    try:
        from app import audit

        audit.record(action, project, target, before=before, after=after, **extra)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Embedding (read only)
# ---------------------------------------------------------------------------


def embedding_state() -> dict:
    from app.embeddings import get_embedder

    emb = get_embedder()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return {"active": getattr(emb, "name", config.EMBEDDING_MODEL),
            "embedder": os.environ.get("KH_EMBEDDER") or "local",
            "backend": os.environ.get("KH_VECTOR_BACKEND") or "sqlite", "maintenance": "", "notice": "",
            "models": [], "jobs": [], "backup_path": os.path.join(root, "embed_backups"), "can_switch": False}


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------


def _projects_with_knowledge() -> list[str]:
    prefix = vs.scope_prefix("knowledge")
    return sorted(n[len(prefix):] for n in vs.list_collection_names() if n.startswith(prefix))


def _flag(v) -> str:
    return "1" if v in (True, 1, "1", "true") else "0" if v in (False, 0, "0", "false") else ""


def _review_item(project: str, it: dict, shared: str) -> dict:
    m = it.get("metadata") or {}
    policy = it["kind"] == "policy_proposal"
    return {"id": it["id"], "kind": it["kind"], "title": it["title"], "text": it["text"], "status": it["status"],
            "action": it["action"], "context": it["context"], "sources": len(it["sources"]),
            "distilled_by": m.get("distilled_by") or "", "distilled_at": it.get("distilled_at") or "",
            "share": False, "can_share": project != shared and not policy, "suggested": _flag(m.get("share_suggested")),
            "contradicts": m.get("contradicts") or None, "contradicts_title": m.get("contradicts_title") or None,
            "contradicted_by": m.get("contradicted_by") or None,
            "contradicted_by_title": m.get("contradicted_by_title") or None,
            "target": f"{m.get('target_action')} / {m.get('target_sub_action')}" if policy else None}


def review() -> dict:
    from app import distill

    shared = distill.shared_project()
    projects, count = [], 0
    for p in _projects_with_knowledge():
        items = distill.items_of(p, OPEN_STATUSES)
        if not items:
            continue
        items.sort(key=lambda i: str(i.get("distilled_at") or ""), reverse=True)
        projects.append({"project": p, "count": len(items), "items": [_review_item(p, i, shared) for i in items]})
        count += len(items)
    return {"shared_project": shared, "projects": projects, "count": count}


class Decision(BaseModel):
    project: str
    id: str
    decision: str
    share: bool = False
    title: Optional[str] = None
    text: Optional[str] = None


class ReviewApply(BaseModel):
    decisions: list[Decision] = Field(default_factory=list)


def _edit(project: str, d: Decision) -> None:
    from app import distill

    it = distill.get_item(project, d.id)
    if it is None or (d.title is None and d.text is None):
        return
    title = (d.title if d.title is not None else it["title"]).strip()[:distill.title_chars()]
    text = (d.text if d.text is not None else it["text"]).strip()[:distill.item_chars()]
    if title and text:
        distill._write(project, d.id, it["metadata"], title, text)


def apply_review(decisions: list[Decision], model=None) -> dict:
    from app import distill

    results = []
    for d in decisions:
        base: dict[str, Any] = {"project": d.project, "id": d.id, "decision": d.decision}
        it = distill.get_item(d.project, d.id)
        if it is None:
            results.append({**base, "result": "unknown", "reason": "no such item"})
            continue
        if d.decision == "approve":
            if it["status"] in OPEN_STATUSES:
                _edit(d.project, d)
            r = distill.approve(d.project, [d.id], [d.id] if d.share else [], model=model)
            row = r["results"][0] if r["results"] else {"result": "unknown"}
            res = {**base, "result": row["result"]}
            if row.get("applied_to"):
                res["applied_to"] = row["applied_to"]
            if r.get("shared"):
                res["shared"] = r["shared"][0].get("result")
                if r["shared"][0].get("shared_id"):
                    res["shared_id"] = r["shared"][0]["shared_id"]
            if str(row["result"]).startswith("not reviewable"):
                res["reason"] = row["result"]
                res["result"] = "refused"
        elif d.decision == "reject":
            r = distill.reject(d.project, [d.id])
            res = {**base, "result": r["results"][0]["result"]}
        else:
            res = {**base, "result": "refused", "reason": "decision must be approve or reject"}
        results.append(res)
        _audit(f"distill.{d.decision}", d.project, d.id, before={"status": it["status"]},
               after={"result": res["result"], "shared": res.get("shared")})
    return {"results": results}


# ---------------------------------------------------------------------------
# Log retention
# ---------------------------------------------------------------------------


class RetentionBody(BaseModel):
    days: Optional[int] = None
    dry_run: bool = True


def _env_days() -> int:
    try:
        return max(int(os.environ.get("KH_LOG_RETENTION_DAYS") or 0), 0)
    except ValueError:
        return 0


def _row_ts(m: dict) -> float:
    from app import hotlog

    for k in ("ts", "created_at", "timestamp"):
        if m.get(k) not in (None, ""):
            return hotlog.parse_ts(m.get(k))
    return 0.0


def retention_logs(days: Optional[int], dry_run: bool) -> dict:
    days = max(days if days is not None else _env_days(), 0)
    if days <= 0:
        return {"days": 0, "dry_run": dry_run, "projects": [], "total": 0, "note": RETENTION_OFF_NOTE}
    cut_dt = datetime.now(timezone.utc) - timedelta(days=days)
    cut = cut_dt.timestamp()
    prefix = vs.scope_prefix("log")
    per, total = [], 0
    for n in vs.list_collection_names():
        if not n.startswith(prefix):
            continue
        h = vs.get_existing(n)
        if h is None:
            continue
        old = [rid for rid, m in vs.meta_rows_vt(h) if 0 < _row_ts(m or {}) < cut]
        if not old:
            continue
        if not dry_run:
            old = vs.delete_vt(old, h)
        per.append({"project": n[len(prefix):], "rows": len(old)})
        total += len(old)
    out = {"days": days, "dry_run": dry_run, "cut": cut_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), "projects": per,
           "total": total}
    if not dry_run:
        for p in per:
            _audit("retention.logs", p["project"], f"older than {days} days", after={"deleted": p["rows"]})
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def attach(app: FastAPI) -> None:
    @app.get("/home/embedding", tags=["home"])
    def home_embedding():
        return embedding_state()

    def _refuse():
        raise HTTPException(status_code=501, detail=EMBEDDING_501)

    for path in ("/home/embedding/models", "/home/embedding/upload", "/home/embedding/models/{model_id}/check",
                 "/home/embedding/models/{model_id}/bench", "/home/embedding/models/{model_id}/delete",
                 "/home/embedding/convert", "/home/embedding/retry", "/home/embedding/maintenance/off",
                 "/home/embedding/rollback"):
        app.add_api_route(path, _refuse, methods=["POST"], tags=["home"])

    @app.get("/home/review", tags=["home"])
    def home_review():
        return review()

    @app.post("/home/review/apply", tags=["home"])
    def home_review_apply(request: Request, body: ReviewApply):
        return apply_review(body.decisions, getattr(request.app.state, "model", None))

    @app.post("/home/retention/logs", tags=["home"])
    def home_retention_logs(body: RetentionBody):
        return retention_logs(body.days, body.dry_run)
