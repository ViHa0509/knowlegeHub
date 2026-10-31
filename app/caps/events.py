"""events (spec §6.4): append-only per-namespace event store with a gapless `seq` cursor, long-poll and stats.

Storage goes through the EventStore port (`app/eventing/store.py`, table `kh_event`, shared with KH's own events).
`append_system` is the core emitter for `kh.*` events.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

from fastapi import Depends, FastAPI, Query, Request

from app import sqldb
from app.caps import core
from app.caps.core import CapError, Ctx

NAME = "events"
VERSION = "1.0.0"
STATUS = "beta"
DEFAULT_ON = True
BATCH_MAX = 500
BODY_MAX = 256 * 1024
READ_MAX = 1000
WAIT_MAX = 25
POLL_S = 0.25
LIMITS = {"batch": BATCH_MAX, "body_bytes": BODY_MAX, "read": READ_MAX, "long_poll_s": WAIT_MAX}
TABLES = ["kh_event"]
LOCAL_TABLES: list[str] = []
CRONS: list = []

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
STATS_FIELDS = ("kind", "project", "run_id", "unit_id", "session_id", "ix_s1", "ix_s2", "ix_s3", "ix_s4")
EVENT_KEYS = {"id", "kind", "project", "ts_ms", "run_id", "unit_id", "session_id", "attempt", "epoch", "body"}


def _store():
    from app.eventing.store import get_store

    return get_store()


def _m1(c) -> None:
    from app.eventing import store

    store.ensure(c)
    sqldb.create_index(c, "kh_event_run", "kh_event", ["ns", "run_id"])
    sqldb.create_index(c, "kh_event_kindts", "kh_event", ["ns", "kind", "ts_ms"])
    sqldb.create_index(c, "kh_event_unit", "kh_event", ["ns", "unit_id"])


def _usage(c, ns: str, type_: str) -> int:
    if not sqldb.table_exists(c, "kh_event"):
        return 0
    return int(c.execute("SELECT COUNT(*) FROM kh_event WHERE ns=? AND kind=?", (ns, type_)).fetchone()[0])


def init(c) -> None:
    sqldb.migrate(c, NAME, [(1, _m1)])
    if _usage not in core.TYPE_USAGE:
        core.TYPE_USAGE.append(_usage)
    if append_system not in core.SYSTEM_EMITTER:
        core.SYSTEM_EMITTER.insert(0, append_system)


# ---------------------------------------------------------------------------
# System events
# ---------------------------------------------------------------------------


def append_system(ns: str, kind: str, body: dict | None = None, ctx: Ctx | None = None, project: str = "") -> None:
    """Core emitter for `kh.*` events (own transaction, never raises)."""
    try:
        actor = ctx.actor() if ctx is not None else {"principal": "kh", "role": "admin"}
        row = {"id": core.new_id(), "kind": kind, "project": project or "", "ts_ms": core.now_ms(),
               "run_id": ctx.run_id if ctx else None, "unit_id": ctx.unit_id if ctx else None,
               "session_id": ctx.session_id if ctx else None, "attempt": ctx.attempt if ctx else None,
               "actor_json": core.dumps(actor), "body_json": core.dumps(body or {})}
        _store().append(ns, [row])
    except Exception as e:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).debug("append_system %s/%s failed: %s", ns, kind, e)


# ---------------------------------------------------------------------------
# Append
# ---------------------------------------------------------------------------


def buffered_ns() -> set[str]:
    return set(core.csv(os.environ.get("KH_EVENTS_BUFFERED_NS") or ""))


def _check_event(ctx: Ctx, ev: Any) -> tuple[dict | None, list[dict], dict | None]:
    """-> (row, errors, type row). Body filters already applied."""
    errs: list[dict] = []
    if not isinstance(ev, dict):
        return None, [{"pointer": "/", "message": "an event must be an object"}], None
    for k in ev:
        if k not in EVENT_KEYS:
            errs.append({"pointer": f"/{k}", "message": "unknown key"})
    kind = ev.get("kind")
    trow = None
    if not isinstance(kind, str) or not kind:
        errs.append({"pointer": "/kind", "message": "kind is required"})
    elif kind == "kh" or kind.startswith("kh."):
        errs.append({"pointer": "/kind", "message": "kh.* kinds are written by KH only"})
    else:
        trow = ctx.type(kind)
        if trow is None or trow.get("kind_of") != "event":
            errs.append({"pointer": "/kind", "message": f"'{kind}' is not an event type of the manifest"})
            trow = None
        elif not ctx.type_may(trow, "write"):
            errs.append({"pointer": "/kind", "message": f"role '{ctx.role}' may not write '{kind}'"})
    eid = ev.get("id")
    if eid is not None and (not isinstance(eid, str) or not ID_RE.match(eid)):
        errs.append({"pointer": "/id", "message": "id must be 1-64 characters of [A-Za-z0-9_-]"})
    body = ev.get("body", {})
    if body is None:
        body = {}
    if core.json_size(body) > BODY_MAX:
        errs.append({"pointer": "/body", "message": f"body is larger than {BODY_MAX} bytes"})
    env: dict[str, Any] = {"run_id": ev.get("run_id", ctx.run_id), "unit_id": ev.get("unit_id", ctx.unit_id),
                           "session_id": ev.get("session_id", ctx.session_id),
                           "attempt": ev.get("attempt", ctx.attempt), "epoch": ev.get("epoch", ctx.epoch)}
    for k in ("run_id", "unit_id", "session_id"):
        v = env[k]
        if v is not None and (not isinstance(v, str) or len(v) > 256):
            errs.append({"pointer": f"/{k}", "message": f"{k} must be a string (<= 256)"})
    for k in ("attempt", "epoch"):
        v = env[k]
        if v is not None and (not isinstance(v, int) or isinstance(v, bool)):
            errs.append({"pointer": f"/{k}", "message": f"{k} must be an integer"})
    ts = ev.get("ts_ms")
    if ts is not None and (not isinstance(ts, int) or isinstance(ts, bool) or ts < 0):
        errs.append({"pointer": "/ts_ms", "message": "ts_ms must be epoch milliseconds"})
    project = ev.get("project")
    if project is not None and not isinstance(project, str):
        errs.append({"pointer": "/project", "message": "project must be a string"})
        project = None
    if project:
        try:
            project = ctx.require_project(project)
        except CapError as e:
            errs.append({"pointer": "/project", "message": str(e.detail)})
    if trow is not None:
        for k in trow.get("required") or []:
            if env.get(k) is None:
                errs.append({"pointer": f"/{k}", "message": f"{k} is required for '{kind}'"})
        schema = ctx.st.schema_for(trow)
        for e in core.validate(schema, body):
            errs.append({"pointer": "/body" + (e["pointer"] if e["pointer"] != "/" else ""), "message": e["message"]})
    if errs:
        return None, errs[:core.MAX_SCHEMA_ERRORS], trow
    row = {"id": eid or core.new_id(), "kind": kind, "project": project or "", "ts_ms": ts,
           "run_id": env["run_id"], "unit_id": env["unit_id"], "session_id": env["session_id"],
           "attempt": env["attempt"], "epoch": env["epoch"], "actor_json": ctx.actor_json(),
           "schema_ver": trow.get("current_ver") if trow else None, "body_json": core.dumps(body)}
    row.update({k: v for k, v in core.index_values(trow, body).items()})
    return row, [], trow


def append(ctx: Ctx, events: list, partial: bool) -> tuple[int, dict]:
    if not events:
        raise CapError("invalid", "no events given")
    if len(events) > BATCH_MAX:
        raise CapError("too_large", f"at most {BATCH_MAX} events per call", extra={"limit": BATCH_MAX})
    rows, rejected, fenced = [], [], False
    for i, ev in enumerate(events):
        if isinstance(ev, dict) and "body" in ev:
            try:
                ev = {**ev, "body": core.apply_body_filters(ctx, "events", ev.get("body"))}
            except CapError as e:
                if not partial:
                    raise
                rejected.append({"index": i, "errors": [{"pointer": "/body", "message": f"{e.code}: {e.detail}"}]})
                continue
        row, errs, trow = _check_event(ctx, ev)
        if errs:
            rejected.append({"index": i, "errors": errs})
            continue
        fenced = fenced or bool(trow and trow.get("fenced"))
        rows.append((i, row))
    if rejected and not partial:
        raise CapError("validation_failed", "some events are not valid", extra={"accepted": [], "rejected": rejected})
    if fenced:
        core.fence_required(ctx, True)
    if not rows:
        return 200, {"accepted": [], "rejected": rejected}
    if ctx.ns in buffered_ns() and not fenced and ctx.lease is None:
        from app.eventing.emit import get_buffer

        get_buffer().publish([(ctx.ns, r) for _, r in rows])
        return 202, {"accepted": [{"index": i, "id": r["id"], "seq": None, "queued": True} for i, r in rows],
                     "rejected": rejected}
    guard = None
    if ctx.lease is not None:
        def guard(c):  # noqa: E306
            return core.fence_ok(c, ctx.ns, ctx.lease, int(ctx.epoch or 0))
    try:
        res = _store().append(ctx.ns, [r for _, r in rows], guard=guard)
    except PermissionError:
        with sqldb.connect() as c:
            raise core.fence_error(c, ctx)
    accepted = []
    dups = []
    for (i, r), out in zip(rows, res):
        accepted.append({"index": i, "id": out["id"], "seq": out["seq"], "duplicate": bool(out["duplicate"])})
        if out["duplicate"]:
            dups.append(out["id"])
    if dups:
        core.emit(ctx.ns, "kh.duplicate", {"what": "event", "ids": dups[:100]}, ctx)
    return 200, {"accepted": accepted, "rejected": rejected}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def hidden_kinds(ctx: Ctx) -> list[str]:
    return [t for t, row in ctx.st.types.items()
            if row.get("kind_of") == "event" and not ctx.type_may(row, "read")]


def read(ctx: Ctx, after_seq: int, kinds: list[str], run_id: str | None, unit_id: str | None,
         limit: int) -> list[dict]:
    filters = {}
    if run_id:
        filters["run_id"] = run_id
    if unit_id:
        filters["unit_id"] = unit_id
    return _store().read(ctx.ns, after_seq=after_seq, kinds=kinds, hidden=hidden_kinds(ctx), filters=filters,
                         limit=limit)


def stats(ctx: Ctx, group_by: list[str], since_ms: int | None) -> list[dict]:
    cols = group_by or ["kind"]
    where, params = ["ns=?"], [ctx.ns]
    if since_ms:
        where.append("ts_ms>=?")
        params.append(int(since_ms))
    hidden = hidden_kinds(ctx)
    if hidden:
        where.append(f"kind NOT IN ({', '.join('?' for _ in hidden)})")
        params += hidden
    sel = ", ".join(cols)
    with sqldb.connect() as c:
        q = (f"SELECT {sel}, COUNT(*) AS n, MIN(seq) AS first_seq, MAX(seq) AS last_seq FROM kh_event "
             f"WHERE {' AND '.join(where)} GROUP BY {sel} ORDER BY n DESC")
        out = []
        for r in c.execute(q, params).fetchall():
            d = {k: r[i] for i, k in enumerate(cols)}
            d["n"] = int(r[len(cols)])
            d["first_seq"] = int(r[len(cols) + 1])
            d["last_seq"] = int(r[len(cols) + 2])
            out.append(d)
        return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def attach(app: FastAPI) -> None:
    tag = ["v1 events"]

    @app.post("/v1/{ns}/events", tags=tag, summary="Append events")
    @core.idempotent
    async def post_events(request: Request, ns: str, partial: bool = Query(False), ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require("events", "append")
        body = await core.read_json(request)
        if isinstance(body, dict) and isinstance(body.get("events"), list) and "kind" not in body:
            events = body["events"]
        elif isinstance(body, dict):
            events = [body]
        else:
            raise CapError("invalid", "send one event object or {events:[...]}")
        status, out = await core.run(append, ctx, events, partial)
        return core.reply(out, status=status)

    @app.get("/v1/{ns}/events:stats", tags=tag, summary="Event counts grouped by fields")
    async def get_stats(ns: str, group_by: str = Query("kind"), since: str | None = Query(None),
                        ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require("events", "read")
        cols = core.csv(group_by) or ["kind"]
        if len(cols) > 4:
            raise CapError("invalid", "group_by takes 1-4 fields")
        bad = [c for c in cols if c not in STATS_FIELDS]
        if bad:
            raise CapError("invalid", f"group_by fields must be among {', '.join(STATS_FIELDS)}")
        groups = await core.run(stats, ctx, list(dict.fromkeys(cols)), core.parse_ms(since))
        return {"ns": ns, "group_by": cols, "groups": groups}

    @app.get("/v1/{ns}/events", tags=tag, summary="Read events after a cursor (long poll)")
    async def get_events(ns: str, after_seq: int = Query(0), kind: str | None = Query(None),
                         run_id: str | None = Query(None), unit_id: str | None = Query(None),
                         limit: str | None = Query(None), wait_s: float = Query(0),
                         ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require("events", "read")
        lim = core.clamp_limit(limit, 100, READ_MAX)
        wait = max(0.0, min(float(WAIT_MAX), float(wait_s or 0)))
        kinds = core.csv(kind)
        deadline = time.monotonic() + wait
        while True:
            evs = await core.run(read, ctx, int(after_seq or 0), kinds, run_id, unit_id, lim)
            if evs or time.monotonic() >= deadline:
                break
            await asyncio.sleep(POLL_S)
        nxt = evs[-1]["seq"] if evs else int(after_seq or 0)
        return {"events": evs, "next_after_seq": nxt}

    @app.get("/v1/{ns}/events/{event_id}", tags=tag, summary="Read one event")
    async def get_event(ns: str, event_id: str, ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require("events", "read")
        ev = await core.run(_store().get, ns, event_id)
        if ev is None or ev["kind"] in hidden_kinds(ctx):
            raise CapError("not_found", f"event '{event_id}' not found")
        return ev
