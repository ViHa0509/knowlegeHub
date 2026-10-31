"""records (spec §6.6): typed JSON records with revisions, If-Match / If-None-Match, RFC 7386 merge patch and a
query with index fields, decay weighting and partitions.

Tables `kh_record` (one row per record) and `kh_record_rev` (one row per write). coord stores effect approvals here
as type `kh.approval` (read-only through this API).
"""
from __future__ import annotations

import random
import re
import time
from typing import Any

from fastapi import Depends, FastAPI, Query, Request

from app import sqldb
from app.caps import core
from app.caps.core import CapError, Ctx

NAME = "records"
VERSION = "1.0.0"
STATUS = "beta"
DEFAULT_ON = True
BODY_MAX = 256 * 1024
QUERY_MAX = 500
REVISIONS_MAX = 1000
PATCH_RETRIES = 16
PATCH_BACKOFF_MS = 200
IN_MAX = 100
ORDER_MAX = 4
PER_PARTITION_DEFAULT = 10
LIMITS = {"body_bytes": BODY_MAX, "query": QUERY_MAX, "revisions": REVISIONS_MAX, "patch_retries": PATCH_RETRIES,
          "in_values": IN_MAX, "order_fields": ORDER_MAX}
TABLES = ["kh_record", "kh_record_rev"]
LOCAL_TABLES: list[str] = []
CRONS: list = []

ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
STATE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
DEFAULT_STATE = "open"
FINAL = "final"
APPROVAL_TYPE = "kh.approval"
APPROVAL_TROW = {"type": APPROVAL_TYPE, "kind_of": "record", "schema_name": None, "current_ver": None,
                 "index_fields": {"key": "ix_s1", "approver": "ix_s2", "decision": "ix_s3"}, "read_roles": [],
                 "write_roles": [], "fenced": False, "require_if_match": False, "state": "active"}
PLAIN_FIELDS = ("id", "project", "state", "rev", "schema_ver", "created_ms", "updated_ms")
IX_COLS = tuple(c for cols in core.IX_SLOTS.values() for c in cols)
OPS = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def _m1(c) -> None:
    sqldb.create_table(
        c, "kh_record",
        "CREATE TABLE IF NOT EXISTS kh_record (ns TEXT NOT NULL, type TEXT NOT NULL, id TEXT NOT NULL, "
        "project TEXT, schema_ver INTEGER, rev INTEGER NOT NULL, state TEXT, ix_s1 TEXT, ix_s2 TEXT, ix_s3 TEXT, "
        "ix_s4 TEXT, ix_n1 REAL, ix_n2 REAL, ix_t1 INTEGER, ix_t2 INTEGER, body_json TEXT, actor_json TEXT, "
        "created_ms INTEGER, updated_ms INTEGER, PRIMARY KEY (ns, type, id))",
        "CREATE COLUMN TABLE kh_record (ns NVARCHAR(64) NOT NULL, type NVARCHAR(128) NOT NULL, "
        "id NVARCHAR(128) NOT NULL, project NVARCHAR(256), schema_ver BIGINT, rev BIGINT NOT NULL, "
        "state NVARCHAR(32), ix_s1 NVARCHAR(256), ix_s2 NVARCHAR(256), ix_s3 NVARCHAR(256), ix_s4 NVARCHAR(256), "
        "ix_n1 DOUBLE, ix_n2 DOUBLE, ix_t1 BIGINT, ix_t2 BIGINT, body_json NCLOB, actor_json NCLOB, "
        "created_ms BIGINT, updated_ms BIGINT, PRIMARY KEY (ns, type, id))")
    sqldb.create_index(c, "kh_record_upd", "kh_record", ["ns", "type", "updated_ms"])
    sqldb.create_index(c, "kh_record_proj", "kh_record", ["ns", "type", "project"])
    sqldb.create_table(
        c, "kh_record_rev",
        "CREATE TABLE IF NOT EXISTS kh_record_rev (ns TEXT NOT NULL, type TEXT NOT NULL, id TEXT NOT NULL, "
        "rev INTEGER NOT NULL, state TEXT, schema_ver INTEGER, body_json TEXT, actor_json TEXT, at_ms INTEGER, "
        "PRIMARY KEY (ns, type, id, rev))",
        "CREATE COLUMN TABLE kh_record_rev (ns NVARCHAR(64) NOT NULL, type NVARCHAR(128) NOT NULL, "
        "id NVARCHAR(128) NOT NULL, rev BIGINT NOT NULL, state NVARCHAR(32), schema_ver BIGINT, body_json NCLOB, "
        "actor_json NCLOB, at_ms BIGINT, PRIMARY KEY (ns, type, id, rev))")


def _usage(c, ns: str, type_: str) -> int:
    if not sqldb.table_exists(c, "kh_record"):
        return 0
    return int(c.execute("SELECT COUNT(*) FROM kh_record WHERE ns=? AND type=?", (ns, type_)).fetchone()[0])


def init(c) -> None:
    sqldb.migrate(c, NAME, [(1, _m1)])
    if _usage not in core.TYPE_USAGE:
        core.TYPE_USAGE.append(_usage)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trow(ctx: Ctx, type_: str, op: str) -> dict:
    ctx.require("records", op)
    if type_ == APPROVAL_TYPE:
        if op != "read":
            raise CapError("forbidden", "kh.* records are written by KH only")
        return APPROVAL_TROW
    t = ctx.type(type_)
    if t is None or t.get("kind_of") != "record":
        raise CapError("not_found", f"'{type_}' is not a record type of the manifest")
    if not ctx.type_may(t, op):
        raise CapError("forbidden", f"role '{ctx.role}' may not {op} '{type_}'")
    return t


def _check_id(rid: str) -> str:
    if not ID_RE.match(rid or ""):
        raise CapError("invalid", "record id must be 1-128 characters of [A-Za-z0-9_.-]")
    return rid


def _check_state(state: str | None) -> str | None:
    if state is None or state == "":
        return None
    if not STATE_RE.match(state):
        raise CapError("invalid", f"state must match {STATE_RE.pattern}")
    return state


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 JSON merge patch."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = merge_patch(out.get(k), v)
    return out


def _view(trow: dict, r) -> dict:
    return {"type": r["type"], "id": r["id"], "project": r["project"] or "", "rev": int(r["rev"]),
            "state": r["state"], "schema_ver": r["schema_ver"], "body": core.loads(r["body_json"], None),
            "index": core.index_view(trow, r), "actor": core.loads(r["actor_json"], None),
            "created_ms": r["created_ms"], "updated_ms": r["updated_ms"]}


def _read(c, ns: str, type_: str, rid: str):
    return c.execute("SELECT * FROM kh_record WHERE ns=? AND type=? AND id=?", (ns, type_, rid)).fetchone()


def _prepare(ctx: Ctx, trow: dict, body: Any) -> Any:
    """Body filters (redaction) outside the transaction, then size and schema checks."""
    core.check_size(body, BODY_MAX)  # before the filters too: never scan an oversized body
    body = core.apply_body_filters(ctx, "records", body)
    core.check_size(body, BODY_MAX)
    errs = core.validate(ctx.st.schema_for(trow), body)
    if errs:
        raise CapError("validation_failed", "record body does not match its schema", extra={"errors": errs})
    return body


def _rev_row(ctx: Ctx, type_: str, rid: str, rev: int, state: str, schema_ver, body_json: str, actor_json: str,
             now: int) -> dict:
    return {"ns": ctx.ns, "type": type_, "id": rid, "rev": rev, "state": state, "schema_ver": schema_ver,
            "body_json": body_json, "actor_json": actor_json, "at_ms": now}


def _write(c, ctx: Ctx, trow: dict, type_: str, rid: str, body: Any, project: str | None, state: str | None,
           expected: int | None, create_only: bool, cur=None) -> tuple[int, Any]:
    """One write inside an open transaction. Returns (status, row)."""
    core.fence(ctx, c, required=bool(trow.get("fenced")))
    now = sqldb.now_ms(c)
    cur = cur if cur is not None else _read(c, ctx.ns, type_, rid)
    body_json = core.dumps(body)
    actor_json = ctx.actor_json()
    ix = core.index_values(trow, body)
    schema_ver = trow.get("current_ver")
    if cur is not None:
        if not ctx.may_project(cur["project"]):
            raise CapError("not_found", f"record {type_}/{rid} not found")
        if create_only:
            raise CapError("duplicate", f"record {type_}/{rid} already exists",
                           extra={"current": int(cur["rev"])}, headers={"ETag": core.etag(int(cur["rev"]))})
        if cur["state"] == FINAL:
            raise CapError("record_final", f"record {type_}/{rid} is final and cannot change")
        if project and project != (cur["project"] or ""):
            raise CapError("invalid", "the project of a record cannot change",
                           extra={"project": cur["project"] or ""})
        new_state = state or cur["state"] or DEFAULT_STATE
        sets = {"schema_ver": schema_ver, "state": new_state, "body_json": body_json, "actor_json": actor_json,
                "updated_ms": now, **ix}
        rev = core.cas_update(c, "kh_record", {"ns": ctx.ns, "type": type_, "id": rid}, sets, expected,
                              ns=ctx.ns, what=f"record {type_}/{rid}")
        sqldb.insert_new(c, "kh_record_rev", _rev_row(ctx, type_, rid, rev, new_state, schema_ver, body_json,
                                                      actor_json, now))
        return 200, _read(c, ctx.ns, type_, rid)
    if expected is not None:
        raise CapError("not_found", f"record {type_}/{rid} not found")
    proj = ctx.require_project(project)
    new_state = state or DEFAULT_STATE
    row = {"ns": ctx.ns, "type": type_, "id": rid, "project": proj, "schema_ver": schema_ver, "rev": 1,
           "state": new_state, **{k: None for k in IX_COLS}, **ix, "body_json": body_json, "actor_json": actor_json,
           "created_ms": now, "updated_ms": now}
    if not sqldb.insert_new(c, "kh_record", row):
        other = _read(c, ctx.ns, type_, rid)
        cur_rev = int(other["rev"]) if other is not None else None
        if create_only:
            raise CapError("duplicate", f"record {type_}/{rid} already exists", extra={"current": cur_rev})
        raise core.write_conflict(ctx.ns, f"record {type_}/{rid}", cur_rev, None)
    sqldb.insert_new(c, "kh_record_rev", _rev_row(ctx, type_, rid, 1, new_state, schema_ver, body_json,
                                                  actor_json, now))
    return 201, _read(c, ctx.ns, type_, rid)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def put_record(ctx: Ctx, trow: dict, type_: str, rid: str, body: Any, project: str | None, state: str | None,
               expected: int | None, create_only: bool) -> tuple[int, dict]:
    body = _prepare(ctx, trow, body)
    core.fence_required(ctx, bool(trow.get("fenced")))
    with sqldb.connect() as c:
        status, r = _write(c, ctx, trow, type_, rid, body, project, state, expected, create_only)
        return status, _view(trow, r)


def patch_record(ctx: Ctx, trow: dict, type_: str, rid: str, patch: Any, project: str | None,
                 state: str | None, expected: int | None) -> dict:
    core.fence_required(ctx, bool(trow.get("fenced")))
    for attempt in range(PATCH_RETRIES):
        with sqldb.connect() as c:
            cur = _read(c, ctx.ns, type_, rid)
        if cur is None or not ctx.may_project(cur["project"]):
            raise CapError("not_found", f"record {type_}/{rid} not found")
        if cur["state"] == FINAL:
            raise CapError("record_final", f"record {type_}/{rid} is final and cannot change")
        body = _prepare(ctx, trow, merge_patch(core.loads(cur["body_json"], None), patch))
        want = expected if expected is not None else int(cur["rev"])
        try:
            with sqldb.connect() as c:
                _, r = _write(c, ctx, trow, type_, rid, body, project, state, want, False)
                return _view(trow, r)
        except CapError as e:
            if e.code != "write_conflict" or expected is not None or attempt == PATCH_RETRIES - 1:
                raise
        time.sleep(random.uniform(0, min(PATCH_BACKOFF_MS, 10 * 2 ** attempt)) / 1000.0)
    raise CapError("write_conflict", f"record {type_}/{rid} kept changing")  # pragma: no cover


def get_record(ctx: Ctx, trow: dict, type_: str, rid: str, rev: int | None) -> dict:
    with sqldb.connect() as c:
        r = _read(c, ctx.ns, type_, rid)
        if r is None or not ctx.may_project(r["project"]):
            raise CapError("not_found", f"record {type_}/{rid} not found")
        out = _view(trow, r)
        if rev is None or rev == out["rev"]:
            return out
        h = c.execute("SELECT * FROM kh_record_rev WHERE ns=? AND type=? AND id=? AND rev=?",
                      (ctx.ns, type_, rid, int(rev))).fetchone()
    if h is None:
        raise CapError("not_found", f"revision {rev} of record {type_}/{rid} not found")
    body = core.loads(h["body_json"], None)
    out.update({"rev": int(h["rev"]), "state": h["state"], "schema_ver": h["schema_ver"], "body": body,
                "index": core.index_view(trow, core.index_values(trow, body)),
                "actor": core.loads(h["actor_json"], None), "updated_ms": h["at_ms"]})
    return out


def list_revisions(ctx: Ctx, trow: dict, type_: str, rid: str, limit: int) -> dict:
    with sqldb.connect() as c:
        r = _read(c, ctx.ns, type_, rid)
        if r is None or not ctx.may_project(r["project"]):
            raise CapError("not_found", f"record {type_}/{rid} not found")
        rows = c.execute(f"SELECT rev, state, schema_ver, body_json, actor_json, at_ms FROM kh_record_rev "
                         f"WHERE ns=? AND type=? AND id=? ORDER BY rev DESC LIMIT {int(limit)}",
                         (ctx.ns, type_, rid)).fetchall()
    revs = [{"rev": int(h[0]), "state": h[1], "schema_ver": h[2], "body": core.loads(h[3], None),
             "actor": core.loads(h[4], None), "at_ms": h[5]} for h in rows]
    return {"type": type_, "id": rid, "current": int(r["rev"]), "revisions": revs, "count": len(revs)}


# -- query ------------------------------------------------------------------


def _column(trow: dict, name: Any) -> str:
    if not isinstance(name, str) or not name:
        raise CapError("invalid", "field names must be strings")
    ixf = trow.get("index_fields") or {}
    if name in ixf:
        return ixf[name]
    if name in PLAIN_FIELDS or name in IX_COLS:
        return name
    raise CapError("invalid", f"unknown query field '{name}' (use an index field, an ix_* column or one of "
                              f"{', '.join(PLAIN_FIELDS)})")


def _scalar(v: Any, what: str):
    if isinstance(v, bool):
        return int(v)
    if v is None or isinstance(v, (str, int, float)):
        return v
    raise CapError("invalid", f"{what}: values must be strings, numbers, booleans or null")


def _where(trow: dict, where: Any) -> tuple[list[str], list]:
    if where is None:
        return [], []
    if not isinstance(where, dict):
        raise CapError("invalid", "where must be an object {field: value | {op: value}}")
    sql, params = [], []
    for f, cond in where.items():
        col = _column(trow, f)
        if not isinstance(cond, dict):
            cond = {"eq": cond}
        if not cond:
            raise CapError("invalid", f"where.{f}: empty condition")
        for op, v in cond.items():
            if op == "in":
                if not isinstance(v, list) or not v or len(v) > IN_MAX:
                    raise CapError("invalid", f"where.{f}.in must be a list of 1-{IN_MAX} values")
                vals = [_scalar(x, f"where.{f}.in") for x in v]
                sql.append(f"{col} IN ({', '.join('?' for _ in vals)})")
                params += vals
            elif op in OPS:
                val = _scalar(v, f"where.{f}.{op}")
                if val is None and op in ("eq", "ne"):
                    sql.append(f"{col} IS {'NOT ' if op == 'ne' else ''}NULL")
                elif val is None:
                    raise CapError("invalid", f"where.{f}.{op}: null is only allowed with eq/ne")
                else:
                    sql.append(f"{col} {OPS[op]} ?")
                    params.append(val)
            else:
                raise CapError("invalid", f"where.{f}: unknown operator '{op}' (eq, ne, gt, gte, lt, lte, in)")
    return sql, params


def _order(trow: dict, order: Any) -> str:
    if order is None or order == "" or order == []:
        order = ["-updated_ms"]
    if isinstance(order, str):
        order = [order]
    if not isinstance(order, list) or len(order) > ORDER_MAX:
        raise CapError("invalid", f"order must be a field name ('-f' = descending) or a list of <= {ORDER_MAX}")
    parts = []
    for o in order:
        if not isinstance(o, str) or not o:
            raise CapError("invalid", "order entries must be field names")
        desc = o.startswith("-")
        parts.append(f"{_column(trow, o.lstrip('-+'))} {'DESC' if desc else 'ASC'}")
    if not any(p.startswith("id ") for p in parts):
        parts.append("id ASC")
    return ", ".join(parts)


def query_records(ctx: Ctx, trow: dict, type_: str, q: dict) -> dict:
    unknown = set(q) - {"where", "project", "state", "order", "limit", "offset", "decay", "partition_by",
                        "per_partition"}
    if unknown:
        raise CapError("invalid", f"unknown query keys: {', '.join(sorted(unknown))}")
    conds, params = _where(trow, q.get("where"))
    sql = ["ns=?", "type=?"] + conds
    params = [ctx.ns, type_] + params
    project = q.get("project")
    if project:
        if not isinstance(project, str) or not ctx.may_project(project):
            raise CapError("forbidden", f"no grant on project '{project}'")
        sql.append("project=?")
        params.append(project)
    else:
        eff = ctx.projects()
        if "*" not in eff:
            vis = [""] + list(eff)
            sql.append(f"(project IS NULL OR project IN ({', '.join('?' for _ in vis)}))")
            params += vis
    state = q.get("state")
    if state:
        states = state if isinstance(state, list) else core.csv(state)
        if not states or len(states) > IN_MAX or not all(isinstance(s, str) for s in states):
            raise CapError("invalid", f"state must be a state or a list of <= {IN_MAX} states")
        sql.append(f"state IN ({', '.join('?' for _ in states)})")
        params += states
    limit = core.clamp_limit(q.get("limit"), 100, QUERY_MAX)
    offset = core.parse_int(q.get("offset"), "offset", 0, 0) or 0
    order = _order(trow, q.get("order"))
    decay = q.get("decay")
    dcol, half = None, None
    if decay is not None:
        if not isinstance(decay, dict):
            raise CapError("invalid", "decay must be {field, half_life_days}")
        dfield = decay.get("field") or "updated_ms"
        dcol = _column(trow, dfield)
        if dcol not in ("created_ms", "updated_ms") and not dcol.startswith("ix_t"):
            raise CapError("invalid", "decay.field must be created_ms, updated_ms or a t index field")
        half = decay.get("half_life_days")
        if isinstance(half, bool) or not isinstance(half, (int, float)) or half <= 0:
            raise CapError("invalid", "decay.half_life_days must be a number > 0")
    pcol = _column(trow, q["partition_by"]) if q.get("partition_by") else None
    per = core.clamp_limit(q.get("per_partition"), PER_PARTITION_DEFAULT, QUERY_MAX) if pcol else None
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        rows = c.execute(f"SELECT * FROM kh_record WHERE {' AND '.join(sql)} ORDER BY {order} "
                         f"LIMIT {int(limit)} OFFSET {int(offset)}", params).fetchall()
    records = []
    for r in rows:
        v = _view(trow, r)
        if dcol is not None:
            ts = r[dcol]
            if ts is None:
                v["age_days"], v["weight"] = None, 0.0
            else:
                age = max(0.0, (now - int(ts)) / 86_400_000.0)
                v["age_days"] = round(age, 6)
                v["weight"] = 0.5 ** (age / float(half))
        records.append((r[pcol] if pcol else None, v))
    out: dict[str, Any] = {"type": type_, "now_ms": now, "limit": limit, "offset": offset}
    if pcol:
        parts: dict[Any, dict] = {}
        kept = []
        for key, v in records:
            p = parts.setdefault(key, {"key": key, "n": 0, "weight_sum": 0.0, "ids": []})
            p["n"] += 1
            p["weight_sum"] += v.get("weight", 1.0) if dcol is not None else 1.0
            if len(p["ids"]) < per:
                p["ids"].append(v["id"])
                kept.append(v)
        for p in parts.values():
            p["weight_sum"] = round(p["weight_sum"], 6)
        out["records"] = kept
        out["partitions"] = list(parts.values())
        out["per_partition"] = per
    else:
        out["records"] = [v for _, v in records]
    out["count"] = len(out["records"])
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def attach(app: FastAPI) -> None:
    tag = ["v1 records"]

    @app.post("/v1/{ns}/records/{type}:query", tags=tag, summary="Query records of a type")
    @core.idempotent
    async def post_query(request: Request, ns: str, type: str, ctx: Ctx = Depends(core.ns_ctx)):
        trow = _trow(ctx, type, "read")
        q = await core.read_object(request)
        return core.reply(await core.run(query_records, ctx, trow, type, q))

    @app.get("/v1/{ns}/records/{type}/{id}:revisions", tags=tag, summary="Revisions of a record (newest first)")
    async def get_revisions(ns: str, type: str, id: str, limit: str | None = Query(None),
                            ctx: Ctx = Depends(core.ns_ctx)):
        trow = _trow(ctx, type, "read")
        lim = core.clamp_limit(limit, 100, REVISIONS_MAX)
        return await core.run(list_revisions, ctx, trow, type, _check_id(id), lim)

    @app.get("/v1/{ns}/records/{type}/{id}", tags=tag, summary="Read a record (?rev= for an older revision)")
    async def get_one(ns: str, type: str, id: str, rev: str | None = Query(None),
                      ctx: Ctx = Depends(core.ns_ctx)):
        trow = _trow(ctx, type, "read")
        out = await core.run(get_record, ctx, trow, type, _check_id(id), core.parse_int(rev, "rev", None, 1))
        return core.reply(out, etag_rev=out["rev"])

    @app.put("/v1/{ns}/records/{type}/{id}", tags=tag, summary="Create or replace a record")
    async def put_one(request: Request, ns: str, type: str, id: str, project: str | None = Query(None),
                      state: str | None = Query(None), ctx: Ctx = Depends(core.ns_ctx)):
        trow = _trow(ctx, type, "write")
        _check_id(id)
        create_only = (request.headers.get("if-none-match") or "").strip() == "*"
        expected = core.require_if_match(request, bool(trow.get("require_if_match")) and not create_only)
        body = await core.read_json(request)
        status, out = await core.run(put_record, ctx, trow, type, id, body, project, _check_state(state),
                                     expected, create_only)
        return core.reply(out, status=status, etag_rev=out["rev"])

    @app.patch("/v1/{ns}/records/{type}/{id}", tags=tag, summary="RFC 7386 merge patch of a record")
    async def patch_one(request: Request, ns: str, type: str, id: str, project: str | None = Query(None),
                        state: str | None = Query(None), ctx: Ctx = Depends(core.ns_ctx)):
        trow = _trow(ctx, type, "write")
        _check_id(id)
        expected = core.require_if_match(request, bool(trow.get("require_if_match")))
        patch = await core.read_json(request)
        out = await core.run(patch_record, ctx, trow, type, id, patch, project, _check_state(state), expected)
        return core.reply(out, etag_rev=out["rev"])
