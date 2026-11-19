"""Change audit (KH 7.1 home Audit tab): table kh_audit, record(), /home/audit, /home/audit/export, /home/rules/history.

Actions: rule.<kind>.add|update|delete (kind policy | kernel), distill.*, home.*, embedding.*, retention.*,
threshold.*, finding.*, flow.*, project_type.*. `record` never raises: a failing audit write never fails the
change it records. Route group `audit`.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, Query
from fastapi.responses import Response

from app import sqldb

log = logging.getLogger(__name__)

EVERY = "every project"
DEFAULT_LIMIT = 200
MAX_LIMIT = 5000

_SQLITE = """CREATE TABLE IF NOT EXISTS kh_audit (
    id TEXT PRIMARY KEY, ts TEXT NOT NULL, actor TEXT, action TEXT NOT NULL, project TEXT, target TEXT,
    detail TEXT)"""
_HANA = """CREATE COLUMN TABLE kh_audit (
    id NVARCHAR(32) PRIMARY KEY, ts NVARCHAR(24) NOT NULL, actor NVARCHAR(320), action NVARCHAR(100) NOT NULL,
    project NVARCHAR(200), target NVARCHAR(1000), detail NCLOB)"""

_ready = {"ok": False}
_lock = threading.Lock()
_last = [0]


def _ensure(c) -> None:
    if not _ready["ok"]:
        sqldb.create_table(c, "kh_audit", _SQLITE, _HANA)
        sqldb.create_index(c, "ix_kh_audit_ts", "kh_audit", ["ts"])
        _ready["ok"] = True


def _new_id() -> str:
    """au_ + 22 hex: 14 hex of a strictly increasing microsecond clock (sorts by time) + 8 random hex."""
    with _lock:
        t = max(time.time_ns() // 1000, _last[0] + 1)
        _last[0] = t
    return "au_" + f"{t:014x}" + os.urandom(4).hex()


def actor() -> str:
    """Signed-in e-mail, else 'local' (open mode)."""
    try:
        from app import auth

        p = auth.current()
        return getattr(p, "email", None) or "local" if p is not None else "local"
    except Exception:  # noqa: BLE001
        return "local"


def policy_target(project: Optional[str], action: str, slot: str = "") -> str:
    return f"{project or EVERY}|{action}|{slot}"


def record(action: str, project: Optional[str] = None, target: str = "", before: Any = None, after: Any = None,
           **extra: Any) -> None:
    """Append one audit row. Never raises."""
    try:
        detail: dict = {}
        if action.startswith("rule.") or before is not None or after is not None:
            detail["before"] = before
            detail["after"] = after
        detail.update(extra)
        row = {"id": _new_id(), "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "actor": actor(),
               "action": action, "project": project or EVERY, "target": target or "",
               "detail": json.dumps(detail, ensure_ascii=False, default=str)}
        with sqldb.connect() as c:
            _ensure(c)
            sqldb.upsert(c, "kh_audit", "id", row)
    except Exception as e:  # noqa: BLE001
        log.warning("audit write failed (%s): %s", action, e)


def _out(r) -> dict:
    try:
        det = json.loads(r["detail"] or "{}")
    except ValueError:
        det = {}
    return {"id": r["id"], "ts": r["ts"], "actor": r["actor"], "action": r["action"], "project": r["project"],
            "target": r["target"], "detail": det}


def _where(action: str, project: str, since: str, until: str, extra: str = "", params: Optional[list] = None):
    conds, ps = [], []
    if action:
        conds.append(f"SUBSTR(action, 1, {len(action)}) = ?")
        ps.append(action)
    if project:
        conds.append("project = ?")
        ps.append(project)
    if since:
        conds.append("ts >= ?")
        ps.append(since)
    if until:
        conds.append("ts <= ?")
        ps.append(until + "T23:59:59Z" if len(until) == 10 else until)
    if extra:
        conds.append(extra)
        ps.extend(params or [])
    return (" WHERE " + " AND ".join(conds)) if conds else "", ps


def query(action: str = "", project: str = "", since: str = "", until: str = "", limit: int = DEFAULT_LIMIT,
          offset: int = 0, oldest_first: bool = False) -> dict:
    limit = DEFAULT_LIMIT if limit <= 0 else min(limit, MAX_LIMIT)
    offset = max(0, offset)
    w, ps = _where(action, project, since, until)
    order = "ASC" if oldest_first else "DESC"
    with sqldb.connect() as c:
        _ensure(c)
        total = int(c.execute(f"SELECT COUNT(*) FROM kh_audit{w}", ps).fetchone()[0])
        rows = c.execute(f"SELECT * FROM kh_audit{w} ORDER BY ts {order}, id {order} LIMIT ? OFFSET ?",
                         ps + [limit, offset]).fetchall()
    return {"total": total, "rows": [_out(r) for r in rows]}


def history(kind: str, key: str) -> list[dict]:
    w, ps = _where(f"rule.{kind}.", "", "", "", "target = ?", [key])
    with sqldb.connect() as c:
        _ensure(c)
        rows = c.execute(f"SELECT * FROM kh_audit{w} ORDER BY ts ASC, id ASC", ps).fetchall()
    return [_out(r) for r in rows]


def attach(app: FastAPI) -> None:
    from app import ctx_util as U

    @app.get("/home/audit", tags=["home"])
    def home_audit(action: str = "", project: str = "", since: str = "", until: str = "",
                   limit: int = DEFAULT_LIMIT, offset: int = 0):
        U.need_maintainer()
        return query(action, project, since, until, limit, offset)

    @app.get("/home/audit/export", tags=["home"])
    def home_audit_export(fmt: str = "csv", action: str = "", project: str = "", since: str = "",
                          until: str = "", limit: int = MAX_LIMIT, offset: int = 0):
        U.need_maintainer()
        rows = query(action, project, since, until, limit, offset)["rows"]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.writer(buf, lineterminator="\n")
            w.writerow(["ts", "actor", "action", "project", "target", "detail"])
            for r in rows:
                w.writerow([r["ts"], r["actor"], r["action"], r["project"], r["target"],
                            json.dumps(r["detail"], ensure_ascii=False)])
            body, ctype, ext = buf.getvalue(), "text/csv; charset=utf-8", "csv"
        else:
            body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
            ctype, ext = "application/x-ndjson", "jsonl"
        return Response(content=body, media_type=ctype,
                        headers={"Content-Disposition": f'attachment; filename="kh-audit-{stamp}.{ext}"'})

    @app.get("/home/rules/history", tags=["home"])
    def home_rules_history(kind: str = Query(...), key: str = Query(...)):
        U.need_maintainer()
        return {"kind": kind, "key": key, "changes": history(kind, key)}
