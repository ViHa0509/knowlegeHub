"""Usage counting and repeats across days (spec §3.12). Route group `usage`.

    key_of(text) -> "action[/object]" | None      learn(day, projects=None) -> report (idempotent)
    repeats(person, days=14, min_days=3)          turn_lines(text, limit=3) -> [str]
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from app import config, sqldb
from app import ctx_config as C
from app import ctx_util as U

log = logging.getLogger(__name__)

WINDOW_DAYS = C.USAGE_WINDOW_DAYS
MIN_DAYS = C.USAGE_MIN_DAYS
SAMPLE_CHARS = C.USAGE_SAMPLE_CHARS
LIST_MAX = C.USAGE_LIST_MAX
KEY_MAX = 100
BATCH = 200
REPEATABLE = frozenset("""sign_in sign_out deploy build release install upgrade configure run_script merge push export
import upload download archive backup restore migrate track_time send notify announce schedule monitor convert copy
move rename screenshot sync seed open_page cleanup invite grant_access index scan benchmark""".split())

_SQLITE = """CREATE TABLE IF NOT EXISTS usage_daily (
    person TEXT NOT NULL, day_utc TEXT NOT NULL, action_key TEXT NOT NULL, hits INTEGER DEFAULT 0,
    sessions TEXT DEFAULT '[]', projects TEXT DEFAULT '[]', sample TEXT,
    PRIMARY KEY (person, day_utc, action_key))"""
_HANA = """CREATE COLUMN TABLE usage_daily (
    person NVARCHAR(200) NOT NULL, day_utc NVARCHAR(10) NOT NULL, action_key NVARCHAR(100) NOT NULL,
    hits INTEGER DEFAULT 0, sessions NVARCHAR(5000) DEFAULT '[]', projects NVARCHAR(5000) DEFAULT '[]',
    sample NVARCHAR(300), PRIMARY KEY (person, day_utc, action_key))"""
_ready = False


def ensure() -> None:
    global _ready
    if _ready:
        return
    with sqldb.connect() as c:
        sqldb.create_table(c, "usage_daily", _SQLITE, _HANA)
    _ready = True


def key_of(text: str | None) -> Optional[str]:
    from app.kernel import tags as T

    if not (text or "").strip():
        return None
    acts = [s["tag"] for s in T.suggest(text, limit=40, facet="action", floor=1.5) if s["tag"] in REPEATABLE]
    if not acts:
        return None
    objs = T.suggest(text, limit=40, facet="object", floor=1.5)
    key = acts[0] + ("/" + objs[0]["tag"] if objs else "")
    return key[:KEY_MAX]


def person_of(meta: dict | None) -> str:
    from app.personal import personal_id

    m = meta or {}
    who = m.get("acting_user") or m.get("author")
    return personal_id(who) if who else "local"


def caller_person() -> str:
    from app.personal import personal_id

    try:
        from app import auth

        k = auth.acting_key()
    except Exception:  # noqa: BLE001
        k = None
    return personal_id(k) if k else "local"


def _day_bounds(day: str) -> tuple[float, float]:
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return d.timestamp(), d.timestamp() + 86400


def _log_lanes(projects: Optional[list[str]] = None) -> list:
    from app import hotlog
    from app import vector_store as vs

    scope = config.HOTLOG_SCOPE
    names = vs.scope_collections_all(scope, None)
    lanes = []
    for n in names:
        pid = n[len(vs.scope_prefix(scope)):]
        if projects and pid not in projects:
            continue
        h = vs.get_existing(n)
        if h is not None:
            lanes.append((h, {"scope": {"$eq": scope}}))
    if hotlog.enabled():
        w = {"scope": {"$eq": scope}}
        if projects:
            w = vs.and_where(w, {"project": {"$in": list(projects)}})
        lanes.append((hotlog.collection(), w))
    return lanes


def learn(day: str, projects: Optional[list[str]] = None) -> dict:
    """Aggregate one UTC day's worklogs into usage_daily (DELETE the day's rows, then upsert)."""
    from app import hotlog
    from app import vector_store as vs

    ensure()
    lo, hi = _day_bounds(day)
    agg: dict[tuple[str, str], dict] = {}
    read = keyed = 0
    seen: set = set()
    for coll, where in _log_lanes(projects):
        ids = []
        for rid, m in vs.meta_rows_vt(coll, where):
            if rid in seen:
                continue
            ts = hotlog.parse_ts(m.get("ts"))
            if lo <= ts < hi:
                ids.append(rid)
                seen.add(rid)
        for i in range(0, len(ids), BATCH):
            for r in vs.get_ids_vt(coll, ids[i:i + BATCH]):
                read += 1
                text = r.get("source_text") or ""
                k = key_of(text)
                if not k:
                    continue
                keyed += 1
                m = r.get("metadata") or {}
                a = agg.setdefault((person_of(m), k), {"hits": 0, "sessions": [], "projects": [], "sample": None})
                a["hits"] += 1
                if m.get("actor") and m["actor"] not in a["sessions"]:
                    a["sessions"].append(m["actor"])
                if m.get("project") and m["project"] not in a["projects"]:
                    a["projects"].append(m["project"])
                if a["sample"] is None:
                    a["sample"] = U.collapse(text)[:SAMPLE_CHARS]
    with sqldb.connect() as c:
        if projects:
            # Only this run's keys are replaced when the run is limited to some projects.
            for (person, k) in agg:
                c.execute("DELETE FROM usage_daily WHERE person=? AND day_utc=? AND action_key=?", (person, day, k))
        else:
            c.execute("DELETE FROM usage_daily WHERE day_utc=?", (day,))
        for (person, k), a in agg.items():
            sqldb.upsert(c, "usage_daily", ["person", "day_utc", "action_key"], {
                "person": person, "day_utc": day, "action_key": k, "hits": a["hits"],
                "sessions": json.dumps(a["sessions"][-LIST_MAX:]), "projects": json.dumps(a["projects"][:LIST_MAX]),
                "sample": a["sample"] or ""})
    return {"day": day, "read": read, "keyed": keyed, "rows": len(agg)}


def learn_recent(days_back: int = 1, projects: Optional[list[str]] = None) -> list[dict]:
    today = datetime.now(timezone.utc).date()
    return [learn((today - timedelta(days=i)).isoformat(), projects) for i in range(days_back, -1, -1)]


def rows_of(person: str, days: int = WINDOW_DAYS) -> list[dict]:
    ensure()
    since = (datetime.now(timezone.utc).date() - timedelta(days=days - 1)).isoformat()
    with sqldb.connect() as c:
        rows = c.execute("SELECT day_utc, action_key, hits, sessions, projects, sample FROM usage_daily "
                         "WHERE person=? AND day_utc>=? ORDER BY day_utc DESC, action_key", (person, since)).fetchall()
    return [{"day": r[0], "key": r[1], "hits": int(r[2] or 0), "sessions": U.jload(r[3], []),
             "projects": U.jload(r[4], []), "sample": r[5] or ""} for r in rows]


def repeats(person: str, days: int = WINDOW_DAYS, min_days: int = MIN_DAYS) -> list[dict]:
    by: dict[str, dict] = {}
    for r in rows_of(person, days):
        a = by.setdefault(r["key"], {"key": r["key"], "days": 0, "total": 0, "last_day": r["day"], "sample": ""})
        a["days"] += 1
        a["total"] += r["hits"]
        if r["day"] >= a["last_day"]:
            a["last_day"] = r["day"]
            a["sample"] = a["sample"] or r["sample"]
        if not a["sample"]:
            a["sample"] = r["sample"]
    out = [a for a in by.values() if a["days"] >= min_days]
    out.sort(key=lambda a: (-a["days"], -a["total"], a["key"]))
    return out


def turn_lines(text: str, limit: int = 3, person: Optional[str] = None) -> list[str]:
    k = key_of(text)
    if not k:
        return []
    act, _, obj = k.partition("/")
    out = []
    for r in repeats(person or caller_person()):
        ra, _, ro = r["key"].partition("/")
        if ra != act:
            continue
        if obj and ro and ro != obj:
            continue
        out.append(f"{r['key']} on {r['days']} days of the last {WINDOW_DAYS} ({r['total']} times, last "
                   f"{r['last_day']})")
        if len(out) >= limit:
            break
    return out


class LearnBody(BaseModel):
    day: Optional[str] = None
    days_back: Optional[int] = None
    projects: Optional[list[str]] = None


def attach(app: FastAPI) -> None:
    ensure()

    @app.get("/usage/repeats", tags=["usage"])
    def usage_repeats(days: int = Query(WINDOW_DAYS, ge=1, le=90), min_days: int = Query(MIN_DAYS, ge=1, le=90)):
        person = caller_person()
        reps = repeats(person, days, min_days)
        return {"person": person, "days": days, "min_days": min_days, "count": len(reps), "repeats": reps}

    @app.get("/usage/mine", tags=["usage"])
    def usage_mine(days: int = Query(WINDOW_DAYS, ge=1, le=90)):
        person = caller_person()
        rows = rows_of(person, days)
        return {"person": person, "days": days, "count": len(rows), "rows": rows}

    @app.post("/usage/learn", tags=["usage"])
    def usage_learn(body: LearnBody):
        U.need_maintainer()
        if body.day:
            try:
                _day_bounds(body.day)
            except ValueError:
                raise HTTPException(status_code=422, detail="day must be YYYY-MM-DD")
            return {"days": [learn(body.day, body.projects)]}
        return {"days": learn_recent(max(0, min(int(body.days_back if body.days_back is not None else 1), 90)),
                                     body.projects)}
