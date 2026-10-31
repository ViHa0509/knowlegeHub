"""Feedback counter and findings (spec §4.7, model SA-1).

The agent mints/increments an issue counter when it notices a recurring mistake; a dumb cron (no AI, no worklog
reading) marks a finding when `count > KH_FINDING_MIN_COUNT` (3); the agent reads findings at the next consult.
Resolving clears issue and finding together.

Helpers for other areas: `list_findings(project) -> [finding dict]` (storage order), `sweep() -> {project: n}`.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import config, sqldb
from app import vector_store as vs

log = logging.getLogger(__name__)

FINDING_PREFIX = "finding__"


def min_count() -> int:
    return config._int("KH_FINDING_MIN_COUNT", 3)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _m1(c):
    sqldb.create_table(
        c, "feedback_issue",
        "CREATE TABLE IF NOT EXISTS feedback_issue (project TEXT NOT NULL, issue_id TEXT NOT NULL, descr TEXT, "
        "count INTEGER NOT NULL DEFAULT 0, opened_at_request INTEGER DEFAULT 0, last_marked_count INTEGER DEFAULT 0, "
        "created_at TEXT, updated_at TEXT, PRIMARY KEY (project, issue_id))",
        "CREATE COLUMN TABLE feedback_issue (project NVARCHAR(256) NOT NULL, issue_id NVARCHAR(256) NOT NULL, "
        "descr NVARCHAR(2000), count INTEGER NOT NULL, opened_at_request INTEGER, last_marked_count INTEGER, "
        "created_at NVARCHAR(40), updated_at NVARCHAR(40), PRIMARY KEY (project, issue_id))")


_ready: set = set()


def _conn():
    c = sqldb.connect()
    key = sqldb._location(c)
    if key not in _ready:
        sqldb.migrate(c, "feedback", [(1, _m1)])
        c.commit()
        _ready.add(key)
    return c


def _issue_dict(r) -> dict:
    return {"issue_id": r["issue_id"], "desc": r["descr"] or "", "count": int(r["count"] or 0),
            "opened_at_request": int(r["opened_at_request"] or 0),
            "last_marked_count": int(r["last_marked_count"] or 0), "created_at": r["created_at"],
            "updated_at": r["updated_at"]}


def _import_legacy(c, project: str) -> None:
    """Import projects/<id>/counter.json once per project (marker row fb:imported:<p>)."""
    if sqldb.counter_add(c, f"fb:imported:{project}", 1) != 1:
        return
    try:
        from app import projects

        legacy = projects.read_counter(project) or {}
    except Exception:  # noqa: BLE001
        return
    total = int(legacy.get("request_total") or 0)
    if total:
        sqldb.counter_add(c, f"fb:req:{project}", total)
    for iid, iss in (legacy.get("issues") or {}).items():
        if not isinstance(iss, dict):
            continue
        sqldb.insert_new(c, "feedback_issue", {
            "project": project, "issue_id": str(iid), "descr": str(iss.get("desc") or "")[:2000],
            "count": int(iss.get("count") or 0), "opened_at_request": int(iss.get("opened_at_request") or 0),
            "last_marked_count": int(iss.get("last_marked_count") or 0),
            "created_at": iss.get("created_at") or _now(), "updated_at": iss.get("updated_at") or _now()})


def request_total(c, project: str) -> int:
    return sqldb.counter_get(c, f"fb:req:{project}")


def bump(project: str, issue_id: str, desc: str = "") -> dict:
    """count=count+1, else insert-new, else update again (atomic under concurrency)."""
    now = _now()
    with _conn() as c:
        _import_legacy(c, project)
        params = (now, project, issue_id)
        if desc:
            cur = c.execute("UPDATE feedback_issue SET count=count+1, descr=?, updated_at=? WHERE project=? AND issue_id=?",
                            (desc[:2000], *params))
        else:
            cur = c.execute("UPDATE feedback_issue SET count=count+1, updated_at=? WHERE project=? AND issue_id=?",
                            params)
        if not cur.rowcount or cur.rowcount <= 0:
            row = {"project": project, "issue_id": issue_id, "descr": (desc or "")[:2000], "count": 1,
                   "opened_at_request": request_total(c, project), "last_marked_count": 0, "created_at": now,
                   "updated_at": now}
            if not sqldb.insert_new(c, "feedback_issue", row):
                c.execute("UPDATE feedback_issue SET count=count+1, updated_at=? WHERE project=? AND issue_id=?",
                          params)
        r = c.execute("SELECT * FROM feedback_issue WHERE project=? AND issue_id=?", (project, issue_id)).fetchone()
    return {"project": project, **_issue_dict(r)}


def tick(project: str, n: int = 1) -> dict:
    with _conn() as c:
        _import_legacy(c, project)
        total = sqldb.counter_add(c, f"fb:req:{project}", int(n))
    return {"project": project, "request_total": total}


def counter(project: str) -> dict:
    with _conn() as c:
        _import_legacy(c, project)
        rows = c.execute("SELECT * FROM feedback_issue WHERE project=? ORDER BY issue_id", (project,)).fetchall()
        total = request_total(c, project)
    return {"request_total": total, "issues": {r["issue_id"]: _issue_dict(r) for r in rows}}


def _findings_coll(project: str, create: bool = False):
    name = vs.part_collection_name("findings", project)
    return vs.get_or_create(name) if create else vs.get_existing(name)


def resolve(project: str, issue_id: str) -> dict:
    with _conn() as c:
        cur = c.execute("DELETE FROM feedback_issue WHERE project=? AND issue_id=?", (project, issue_id))
        issue_cleared = bool(cur.rowcount and cur.rowcount > 0)
    finding_cleared = False
    h = _findings_coll(project)
    if h is not None:
        finding_cleared = bool(vs.delete_vt([FINDING_PREFIX + issue_id], h))
    return {"issue_id": issue_id, "resolved": issue_cleared or finding_cleared, "issue_cleared": issue_cleared,
            "finding_cleared": finding_cleared}


def _write_finding(project: str, iss: dict, model=None) -> None:
    from app.embeddings import get_embedder

    m = model or get_embedder()
    desc = iss["desc"] or iss["issue_id"]
    vec = m.encode(desc, normalize_embeddings=True)
    meta = {"kind": "finding", "scope": "findings", "knowledge_type": "quick", "project": project,
            "issue_id": iss["issue_id"], "desc": desc, "count": iss["count"], "status": "open", "ts": _now()}
    vs.upsert_one(_findings_coll(project, create=True), FINDING_PREFIX + iss["issue_id"], vec, desc, meta)


def sweep(model=None) -> dict:
    """Mark findings for issues with count > min and count > last_marked_count. Returns {project: n}."""
    marked: dict[str, int] = {}
    floor = min_count()
    with _conn() as c:
        rows = c.execute("SELECT * FROM feedback_issue WHERE count > ? AND count > last_marked_count",
                         (floor,)).fetchall()
    for r in rows:
        iss = _issue_dict(r)
        with _conn() as c:
            cur = c.execute("UPDATE feedback_issue SET last_marked_count=? WHERE project=? AND issue_id=? "
                            "AND last_marked_count < ?", (iss["count"], r["project"], iss["issue_id"], iss["count"]))
            won = bool(cur.rowcount and cur.rowcount > 0)
        if not won:
            continue
        try:
            _write_finding(r["project"], iss, model)
            marked[r["project"]] = marked.get(r["project"], 0) + 1
        except Exception as e:  # noqa: BLE001
            log.warning("finding write failed for %s/%s: %s", r["project"], iss["issue_id"], e)
    return marked


def _finding(row: dict) -> dict:
    m = row.get("metadata") or {}
    return {"id": row["vector_id"], "issue_id": m.get("issue_id"), "desc": m.get("desc"), "count": m.get("count"),
            "status": m.get("status"), "ts": m.get("ts"), "text": row.get("source_text")}


def list_findings(project: str) -> list[dict]:
    h = _findings_coll(project)
    if h is None:
        return []
    return [_finding(r) for r in vs.get_vt(h)]


def query_findings(project: str, text: str, k: int = 5, threshold: float = 0.35, model=None) -> list[dict]:
    from app.embeddings import get_embedder

    h = _findings_coll(project)
    if h is None or not text:
        return []
    vec = (model or get_embedder()).encode(text, normalize_embeddings=True)
    out = []
    for hit in vs.query_vt(h, vec, k):
        if hit["score"] >= threshold:
            f = _finding(hit)
            f["score"] = round(float(hit["score"]), 4)
            out.append(f)
    return out


def _on_project_delete(pid: str) -> dict:
    with _conn() as c:
        cur = c.execute("DELETE FROM feedback_issue WHERE project=?", (pid,))
        n = int(cur.rowcount or 0)
        c.execute("DELETE FROM kh_counter WHERE name IN (?, ?)", (f"fb:req:{pid}", f"fb:imported:{pid}"))
    return {"feedback_issues_deleted": n}


# ---------------------------------------------------------------------------
# Cron
# ---------------------------------------------------------------------------

_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def _loop(model) -> None:
    interval = max(1, int(config.FEEDBACK_CRON_SECONDS))
    if _stop.wait(min(interval, 30)):
        return
    while True:
        try:
            sqldb.run_under_lease("cron.feedback", interval * 1.5, lambda: sweep(model))
        except Exception as e:  # noqa: BLE001
            log.warning("feedback cron failed: %s", e)
        if _stop.wait(interval):
            return


def start_cron(model=None) -> bool:
    global _thread
    if _thread is not None and _thread.is_alive():
        return False
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(model,), name="feedback-cron", daemon=True)
    _thread.start()
    return True


def start(app: FastAPI) -> None:
    start_cron(getattr(app.state, "model", None))


def stop() -> None:
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=2)
    _thread = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class IssueRequest(BaseModel):
    project: str
    issue_id: str
    desc: str = ""


class ResolveRequest(BaseModel):
    project: str
    issue_id: str


class TickRequest(BaseModel):
    project: str
    n: int = 1


class FindingsQuery(BaseModel):
    project: str
    intent: Optional[str] = None
    query: Optional[str] = None
    k: int = 5
    threshold: float = 0.35


def attach(app: FastAPI) -> None:
    try:
        with _conn():
            pass
    except Exception as e:  # noqa: BLE001
        log.warning("feedback tables: %s", e)
    try:
        from app import projects

        projects.on_delete(_on_project_delete)
    except Exception:  # noqa: BLE001
        pass

    @app.post("/feedback/issue", tags=["feedback"])
    def feedback_issue(body: IssueRequest):
        if not body.issue_id.strip():
            raise HTTPException(status_code=422, detail="issue_id must not be empty")
        return bump(body.project, body.issue_id.strip(), body.desc)

    @app.post("/feedback/reset", tags=["feedback"])
    def feedback_reset(body: ResolveRequest):
        return resolve(body.project, body.issue_id)

    @app.post("/feedback/resolve", tags=["feedback"])
    def feedback_resolve(body: ResolveRequest):
        return resolve(body.project, body.issue_id)

    @app.post("/feedback/tick", tags=["feedback"])
    def feedback_tick(body: TickRequest):
        return tick(body.project, body.n)

    @app.get("/feedback/counter", tags=["feedback"])
    def feedback_counter(project: str):
        return counter(project)

    @app.post("/feedback/sweep", tags=["feedback"])
    def feedback_sweep():
        return {"marked": sweep(getattr(app.state, "model", None))}

    @app.get("/findings/list", tags=["feedback"])
    def findings_list(project: str):
        f = list_findings(project)
        return {"project": project, "count": len(f), "findings": f}

    @app.post("/findings/query", tags=["feedback"])
    def findings_query(body: FindingsQuery):
        text = body.intent or body.query or ""
        if not text.strip():
            raise HTTPException(status_code=422, detail="give intent or query")
        f = query_findings(body.project, text, max(1, body.k), body.threshold, getattr(app.state, "model", None))
        return {"project": body.project, "count": len(f), "findings": f}
