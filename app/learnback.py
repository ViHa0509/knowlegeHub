"""Learn back: the in-process distil runner (spec §4.4).

Runs the worker's distil job (and optionally promote) inside the KH process with in-process calls instead of HTTP.
Items land as `proposed` and wait on /home > Lesson learn.

Enablement: `KH_LEARNBACK_LLM` in (claude_cli, dry, anthropic); unset = off (the page shows the reason). Also needs
`worker/kh_worker/jobs/distil.py` next to the app or under `KH_WORKER_PATH`. Env: KH_LEARNBACK_MODEL,
KH_LEARNBACK_TZ (Asia/Ho_Chi_Minh), KH_LEARNBACK_TICK_S (30), KH_LEARNBACK_CAP_RUN_USD (5).

Test seams: `CLOCK` (callable -> aware UTC datetime) and `LLM_FACTORY` (callable(kind, model) -> llm).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Callable, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import config, sqldb

log = logging.getLogger(__name__)

LLMS = ("claude_cli", "dry", "anthropic")
KINDS = ("now", "once", "daily", "weekly")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SUMMARY_MAX = 200_000
_FIXED = {"Asia/Ho_Chi_Minh": 7, "Asia/Bangkok": 7, "UTC": 0, "Etc/UTC": 0}

CLOCK: Callable[[], datetime] = lambda: datetime.now(timezone.utc)  # noqa: E731
LLM_FACTORY: Optional[Callable[[str, str], object]] = None


def llm_kind() -> str | None:
    v = (config._env("KH_LEARNBACK_LLM") or "").strip().lower()
    return v or None


def model() -> str:
    return config._env("KH_LEARNBACK_MODEL") or "claude-opus-5-5"


def tz_name() -> str:
    return config._env("KH_LEARNBACK_TZ", "Asia/Ho_Chi_Minh") or "Asia/Ho_Chi_Minh"


def tick_s() -> float:
    return config._float("KH_LEARNBACK_TICK_S", 30)


def cap_run() -> float:
    return config._float("KH_LEARNBACK_CAP_RUN_USD", 5)


def zone(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        if name in _FIXED:
            return timezone(timedelta(hours=_FIXED[name]), name)
        raise ValueError(f"unknown time zone '{name}'")


def worker_path() -> str | None:
    cands = [config._env("KH_WORKER_PATH"), str(config.ROOT / "worker"), str(config.ROOT)]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "kh_worker", "jobs", "distil.py")):
            return c
    return None


def status() -> tuple[bool, str]:
    k = llm_kind()
    if not k:
        return False, "KH_LEARNBACK_LLM is not set (claude_cli, dry or anthropic)"
    if k not in LLMS:
        return False, f"KH_LEARNBACK_LLM={k} is not one of claude_cli, dry, anthropic"
    if worker_path() is None:
        return False, "worker code not found (worker/kh_worker next to the app or KH_WORKER_PATH)"
    return True, ""


def _import_worker():
    p = worker_path()
    if p and p not in sys.path:
        sys.path.insert(0, p)
    import kh_worker.budget as budget  # noqa: F401
    import kh_worker.jobs as jobs
    import kh_worker.llm as llm

    return jobs, llm, budget


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

_ready: set = set()


def _m1(c):
    sqldb.create_table(
        c, "learnback_schedule",
        "CREATE TABLE IF NOT EXISTS learnback_schedule (id TEXT PRIMARY KEY, projects TEXT, kind TEXT, at_spec TEXT, "
        "next_utc TEXT, enabled INTEGER DEFAULT 1, created_by TEXT, created_at TEXT, last_run TEXT)",
        "CREATE COLUMN TABLE learnback_schedule (id NVARCHAR(32) PRIMARY KEY, projects NCLOB, kind NVARCHAR(16), "
        "at_spec NVARCHAR(64), next_utc NVARCHAR(40), enabled INTEGER DEFAULT 1, created_by NVARCHAR(256), "
        "created_at NVARCHAR(40), last_run NVARCHAR(40))")
    sqldb.create_table(
        c, "learnback_run",
        "CREATE TABLE IF NOT EXISTS learnback_run (id TEXT PRIMARY KEY, trigger_kind TEXT, projects TEXT, state TEXT, "
        "started TEXT, finished TEXT, created_by TEXT, summary TEXT)",
        "CREATE COLUMN TABLE learnback_run (id NVARCHAR(32) PRIMARY KEY, trigger_kind NVARCHAR(16), projects NCLOB, "
        "state NVARCHAR(16), started NVARCHAR(40), finished NVARCHAR(40), created_by NVARCHAR(256), summary NCLOB)")


def _conn():
    c = sqldb.connect()
    key = sqldb._location(c)
    if key not in _ready:
        sqldb.migrate(c, "learnback", [(1, _m1)])
        c.execute("UPDATE learnback_run SET state='interrupted' WHERE state='running'")
        c.commit()
        _ready.add(key)
    return c


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------


def next_time(kind: str, at: str | None, now: datetime | None = None, tz: str | None = None) -> datetime | None:
    """Next fire time (UTC) of a schedule; local times are in KH_LEARNBACK_TZ."""
    now = now or CLOCK()
    z = zone(tz or tz_name())
    local_now = now.astimezone(z)
    a = (at or "").strip().lower()
    if kind == "now":
        return now
    if kind == "once":
        try:
            d = datetime.strptime(a, "%Y-%m-%dt%H:%M")
        except ValueError:
            raise ValueError("once needs YYYY-MM-DDTHH:MM (local time)")
        return d.replace(tzinfo=z).astimezone(timezone.utc)
    if kind == "daily":
        try:
            hh, mm = (int(x) for x in a.split(":"))
        except ValueError:
            raise ValueError("daily needs HH:MM")
        cand = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= local_now:
            cand += timedelta(days=1)
        return cand.astimezone(timezone.utc)
    if kind == "weekly":
        parts = a.split()
        if len(parts) != 2 or parts[0] not in DAYS:
            raise ValueError("weekly needs '<day> HH:MM', e.g. 'fri 00:00'")
        hh, mm = (int(x) for x in parts[1].split(":"))
        wd = DAYS.index(parts[0])
        cand = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        cand += timedelta(days=(wd - local_now.weekday()) % 7)
        if cand <= local_now:
            cand += timedelta(days=7)
        return cand.astimezone(timezone.utc)
    raise ValueError(f"kind must be one of {', '.join(KINDS)}")


def _sched_dict(r) -> dict:
    return {"id": r["id"], "projects": json.loads(r["projects"] or "[]"), "kind": r["kind"], "at": r["at_spec"],
            "next_utc": r["next_utc"], "enabled": bool(r["enabled"]), "created_by": r["created_by"],
            "created_at": r["created_at"], "last_run": r["last_run"]}


def schedules() -> list[dict]:
    with _conn() as c:
        return [_sched_dict(r) for r in c.execute("SELECT * FROM learnback_schedule ORDER BY created_at").fetchall()]


def add_schedule(projects: list[str], kind: str, at: str | None, by: str) -> dict:
    nxt = next_time(kind, at)
    row = {"id": "lbs_" + uuid.uuid4().hex[:10], "projects": json.dumps(projects), "kind": kind, "at_spec": at or "",
           "next_utc": _iso(nxt), "enabled": 1, "created_by": by, "created_at": _iso(CLOCK()), "last_run": None}
    with _conn() as c:
        sqldb.insert_new(c, "learnback_schedule", row)
        r = c.execute("SELECT * FROM learnback_schedule WHERE id=?", (row["id"],)).fetchone()
    return _sched_dict(r)


def delete_schedule(sid: str) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM learnback_schedule WHERE id=?", (sid,))
        return bool(cur.rowcount and cur.rowcount > 0)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

_run_lock = threading.Lock()
_running: dict[str, dict] = {}


def runs(limit: int = 20) -> list[dict]:
    with _conn() as c:
        rs = c.execute("SELECT * FROM learnback_run ORDER BY started DESC LIMIT ?", (int(limit),)).fetchall()
    out = []
    for r in rs:
        try:
            summ = json.loads(r["summary"] or "null")
        except ValueError:
            summ = r["summary"]
        out.append({"id": r["id"], "trigger": r["trigger_kind"], "projects": json.loads(r["projects"] or "[]"),
                    "state": r["state"], "started": r["started"], "finished": r["finished"],
                    "created_by": r["created_by"], "summary": summ})
    return out


class _LocalKH:
    """In-process twin of the worker's KHClient."""

    def __init__(self, by: str, mdl=None):
        self.by = by
        self.model = mdl
        self.events: list[dict] = []

    def distill_next(self, project: str, limit: int | None = None) -> dict:
        from app import distill
        from app import vector_store as vs

        lanes = distill.log_lanes(project)
        fh = vs.get_existing(vs.part_collection_name("findings", project))
        if fh is not None:
            lanes = lanes + [(fh, None)]
        return distill.serve_next(project, limit, caller=None, lanes=lanes, model=self.model)

    def distill_submit(self, body: dict) -> dict:
        from app import distill
        from app import vector_store as vs

        lanes = distill.log_lanes(body.get("project"))
        fh = vs.get_existing(vs.part_collection_name("findings", body.get("project")))
        if fh is not None:
            lanes = lanes + [(fh, None)]
        return distill.submit(body, caller=None, lanes=lanes, model=self.model, caller_label=f"learnback:{self.by}")

    def distill_approve(self, project, ids, share=None):
        from app import distill

        return distill.approve(project, ids, share)

    def promote_next(self) -> dict:
        from app import promote

        return promote.next_payload()

    def promote_submit(self, body: dict) -> dict:
        from app import promote

        return promote.submit(body, None, self.model)

    def event(self, kind: str, body: dict) -> None:
        self.events.append({"kind": kind, "body": body})


def _make_llm(kind: str, mdl: str):
    if LLM_FACTORY is not None:
        return LLM_FACTORY(kind, mdl)
    _jobs, llm, _b = _import_worker()
    return llm.make_llm(kind, mdl)


def _execute(run_id: str, projects: list[str], by: str, app_model=None) -> dict:
    jobs, _llm_mod, budget_mod = _import_worker()
    from kh_worker.jobs import distil as distil_job
    from kh_worker.llm import Metered

    kind, mdl = llm_kind(), model()
    llm = _make_llm(kind, mdl)
    budget = budget_mod.Budget(cap_run_usd=cap_run(), cap_month_usd=1e12)
    metered = Metered(llm, budget)
    kh = _LocalKH(by, app_model)
    real = [p for p in projects if p != "common"]
    summary: dict = {"status": "ok", "projects": {}, "llm": kind, "model": getattr(llm, "model", mdl)}
    try:
        ctx = jobs.Ctx(kh=kh, llm=metered, mode="on", projects=real)
        summary["projects"] = distil_job.run(ctx).get("projects", {})
        if "common" in projects:
            from app import promote

            if promote.enabled():
                from kh_worker.jobs import promote as promote_job

                summary["promote"] = promote_job.run(jobs.Ctx(kh=kh, llm=metered, mode="on", projects=["common"]))
            else:
                summary["promote"] = {"status": "skipped", "reason": "promote is off"}
        try:
            from app import usage  # type: ignore

            # usage reads every project's log lanes itself (yesterday + today, no model)
            summary["usage"] = usage.learn_recent(1, None)
        except Exception as e:  # noqa: BLE001
            summary["usage"] = {"status": "skipped", "reason": str(e)[:200]}
    except Exception as e:  # noqa: BLE001
        log.exception("learn back run failed")
        summary["status"] = type(e).__name__ if type(e).__name__ in ("BudgetExceeded",) else "failed"
        summary["error"] = str(e)[:500]
    summary["budget"] = budget.summary()
    return summary


def _finish(run_id: str, summary: dict) -> None:
    state = "done" if summary.get("status") == "ok" else ("failed" if summary.get("status") == "failed"
                                                          else str(summary.get("status")))
    raw = json.dumps(summary, default=str)
    if len(raw) > SUMMARY_MAX:
        raw = json.dumps({"status": summary.get("status"), "truncated": True, "budget": summary.get("budget")})
    with _conn() as c:
        c.execute("UPDATE learnback_run SET state=?, finished=?, summary=? WHERE id=?",
                  (state, _iso(CLOCK()), raw, run_id))


def start_run(projects: list[str], by: str, trigger: str = "now", wait: bool = False, app_model=None) -> dict:
    ok, reason = status()
    if not ok:
        raise HTTPException(status_code=409, detail=f"learn back is off: {reason}")
    if not projects:
        raise HTTPException(status_code=422, detail="name at least one project")
    with _run_lock:
        if _running:
            raise HTTPException(status_code=409, detail="a learn back run is already going")
        run_id = "lbr_" + uuid.uuid4().hex[:10]
        _running[run_id] = {"id": run_id, "projects": projects, "started": _iso(CLOCK()), "created_by": by}
    with _conn() as c:
        sqldb.insert_new(c, "learnback_run", {"id": run_id, "trigger_kind": trigger, "projects": json.dumps(projects),
                                              "state": "running", "started": _iso(CLOCK()), "finished": None,
                                              "created_by": by, "summary": None})

    def body():
        try:
            summ = _execute(run_id, projects, by, app_model)
        except Exception as e:  # noqa: BLE001
            summ = {"status": "failed", "error": str(e)[:500]}
        try:
            _finish(run_id, summ)
        finally:
            with _run_lock:
                _running.pop(run_id, None)

    if wait:
        body()
    else:
        threading.Thread(target=body, name=f"learnback-{run_id}", daemon=True).start()
    return {"id": run_id, "projects": projects, "state": "done" if wait else "running"}


def tick(wait: bool = False) -> list[str]:
    """Fire due enabled schedules if no run is going. once/now disable after firing; others compute next."""
    if not status()[0] or _running:
        return []
    now = CLOCK()
    fired = []
    for s in schedules():
        if not s["enabled"]:
            continue
        nxt = _parse_iso(s["next_utc"])
        if nxt is None or nxt > now:
            continue
        try:
            r = start_run(s["projects"], s["created_by"] or "schedule", trigger=s["kind"], wait=wait)
        except HTTPException:
            break
        fired.append(r["id"])
        with _conn() as c:
            if s["kind"] in ("once", "now"):
                c.execute("UPDATE learnback_schedule SET enabled=0, last_run=? WHERE id=?", (_iso(now), s["id"]))
            else:
                c.execute("UPDATE learnback_schedule SET next_utc=?, last_run=? WHERE id=?",
                          (_iso(next_time(s["kind"], s["at"], now)), _iso(now), s["id"]))
        break  # one run at a time
    return fired


_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def _loop():
    while not _stop.wait(tick_s()):
        try:
            tick()
        except Exception as e:  # noqa: BLE001
            log.warning("learn back tick failed: %s", e)


def start(app: FastAPI) -> None:  # noqa: ARG001
    global _thread
    if llm_kind() not in LLMS:
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="kh-learnback-scheduler", daemon=True)
    _thread.start()


def stop() -> None:
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=2)
    _thread = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    projects: list[str] = Field(default_factory=list)


class ScheduleRequest(BaseModel):
    projects: list[str] = Field(default_factory=list)
    kind: str
    at: Optional[str] = None


def _by() -> str:
    from app import auth

    p = auth.current()
    return getattr(p, "email", None) or "local"


def attach(app: FastAPI) -> None:
    @app.get("/home/learnback", tags=["learnback"])
    def learnback_status():
        ok, reason = status()
        out = {"enabled": ok, "reason": reason, "llm": llm_kind(), "model": model(), "tz": tz_name(),
               "running": list(_running.values()), "schedules": [], "runs": []}
        if ok:
            out["schedules"] = schedules()
            out["runs"] = runs()
        return out

    @app.post("/home/learnback/run", tags=["learnback"])
    def learnback_run(body: RunRequest):
        return start_run(body.projects, _by(), app_model=getattr(app.state, "model", None))

    @app.post("/home/learnback/schedules", tags=["learnback"])
    def learnback_schedule(body: ScheduleRequest):
        ok, reason = status()
        if not ok:
            raise HTTPException(status_code=409, detail=f"learn back is off: {reason}")
        if body.kind not in KINDS:
            raise HTTPException(status_code=422, detail=f"kind must be one of {', '.join(KINDS)}")
        if not body.projects:
            raise HTTPException(status_code=422, detail="name at least one project")
        try:
            return add_schedule(body.projects, body.kind, body.at, _by())
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    @app.post("/home/learnback/schedules/{sid}/delete", tags=["learnback"])
    def learnback_schedule_delete(sid: str):
        if not delete_schedule(sid):
            raise HTTPException(status_code=404, detail="no such schedule")
        return {"deleted": sid}
