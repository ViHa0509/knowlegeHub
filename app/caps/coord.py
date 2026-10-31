"""coord (spec §6.3): leases with fencing epochs, the Idempotency-Key store, side-effect ledger with optional
human approval of high-risk effects.

High-risk approval: `KH_EFFECT_APPROVAL` = manifest (default: gate only a ns whose manifest has
`settings.effects`) | on | off. A claim whose key starts with a prefix of `settings.effects.approval_required`
(`*` = all; default prefixes below) needs a `kh.approval` record with decision approve, written by an approver
still listed in `settings.effects.confirm_policy.emails` (fallback `settings.items.confirm_policy.emails`).
"""
from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Query, Request

from app import sqldb
from app.caps import core
from app.caps.core import CapError, Ctx

NAME = "coord"
VERSION = "1.0.0"
STATUS = "beta"
DEFAULT_ON = True
LEASE_TTL_MIN, LEASE_TTL_MAX, LEASE_TTL_DEFAULT = 5, 300, 30
EFFECT_TTL_MIN, EFFECT_TTL_MAX, EFFECT_TTL_DEFAULT = 1, 86400, 300
NAME_MAX = 200
LIST_MAX = 1000
NOTE_MAX = 2000
APPROVAL_PREFIXES = ["delete/", "deploy/prod", "send/external", "merge/"]
APPROVAL_TYPE = "kh.approval"
LIMITS = {"lease_ttl_s": [LEASE_TTL_MIN, LEASE_TTL_MAX], "lease_ttl_default_s": LEASE_TTL_DEFAULT,
          "effect_ttl_s": [EFFECT_TTL_MIN, EFFECT_TTL_MAX], "effect_ttl_default_s": EFFECT_TTL_DEFAULT,
          "idempotency_hours_default": core.IDEM_DEFAULT_H, "idempotency_hours_max": core.IDEM_MAX_H,
          "idempotency_takeover_s": core.IDEM_TAKEOVER_MS // 1000, "lease_name_chars": NAME_MAX}
TABLES = ["kh_idem", "kh_effect"]
LOCAL_TABLES: list[str] = []
LEASE_VERBS = ("acquire", "renew", "release")
EFFECT_VERBS = ("claim", "complete", "fail", "approve")


def _m1(c) -> None:
    sqldb.create_table(
        c, "kh_idem",
        "CREATE TABLE IF NOT EXISTS kh_idem (ns TEXT NOT NULL, principal TEXT NOT NULL, idem_key TEXT NOT NULL, "
        "request_sha256 TEXT, state TEXT NOT NULL, status_code INTEGER, response_json TEXT, created_ms INTEGER, "
        "updated_ms INTEGER, PRIMARY KEY (ns, principal, idem_key))",
        "CREATE COLUMN TABLE kh_idem (ns NVARCHAR(64) NOT NULL, principal NVARCHAR(256) NOT NULL, "
        "idem_key NVARCHAR(256) NOT NULL, request_sha256 NVARCHAR(64), state NVARCHAR(16) NOT NULL, "
        "status_code INTEGER, response_json NCLOB, created_ms BIGINT, updated_ms BIGINT, "
        "PRIMARY KEY (ns, principal, idem_key))")
    sqldb.create_index(c, "kh_idem_created", "kh_idem", ["created_ms"])
    sqldb.create_table(
        c, "kh_effect",
        "CREATE TABLE IF NOT EXISTS kh_effect (ns TEXT NOT NULL, effect_key TEXT NOT NULL, holder TEXT, "
        "state TEXT NOT NULL, result_json TEXT, claimed_until_ms INTEGER, attempts INTEGER NOT NULL DEFAULT 1, "
        "rev INTEGER NOT NULL DEFAULT 1, created_ms INTEGER, updated_ms INTEGER, PRIMARY KEY (ns, effect_key))",
        "CREATE COLUMN TABLE kh_effect (ns NVARCHAR(64) NOT NULL, effect_key NVARCHAR(512) NOT NULL, "
        "holder NVARCHAR(256), state NVARCHAR(16) NOT NULL, result_json NCLOB, claimed_until_ms BIGINT, "
        "attempts BIGINT NOT NULL, rev BIGINT NOT NULL, created_ms BIGINT, updated_ms BIGINT, "
        "PRIMARY KEY (ns, effect_key))")


def init(c) -> None:
    sqldb.migrate(c, NAME, [(1, _m1)])
    core.idem_ready(True)
    if _confirmer_resolver not in core.CALLER_RESOLVERS:
        core.CALLER_RESOLVERS.append(_confirmer_resolver)


# ---------------------------------------------------------------------------
# Cron
# ---------------------------------------------------------------------------


def purge_idempotency() -> int:
    with sqldb.connect() as c:
        cut = sqldb.now_ms(c) - core.IDEM_MAX_H * 3_600_000
        cur = c.execute("DELETE FROM kh_idem WHERE created_ms < ?", (cut,))
        return int(cur.rowcount or 0)


CRONS = [("cron.idempotency_purge", 3600, purge_idempotency)]


# ---------------------------------------------------------------------------
# Approval policy
# ---------------------------------------------------------------------------


def approval_mode() -> str:
    v = (os.environ.get("KH_EFFECT_APPROVAL") or "manifest").strip().lower()
    return v if v in ("manifest", "on", "off") else "manifest"


def approvers(st: core.NsState) -> list[str]:
    eff = st.settings("effects")
    emails = ((eff.get("confirm_policy") or {}).get("emails")) if isinstance(eff, dict) else None
    if not emails:
        emails = ((st.settings("items").get("confirm_policy") or {}).get("emails"))
    return [str(e).strip().lower() for e in (emails or []) if str(e).strip()]


def approval_prefixes(st: core.NsState) -> list[str] | None:
    """Prefixes gated in this ns, or None when the ns is not gated."""
    mode = approval_mode()
    if mode == "off":
        return None
    eff = st.settings().get("effects")
    if mode == "manifest" and not isinstance(eff, dict):
        return None
    pre = (eff or {}).get("approval_required") if isinstance(eff, dict) else None
    if pre is None:
        return list(APPROVAL_PREFIXES)
    if isinstance(pre, str):
        pre = [pre]
    return [str(p) for p in pre]


def needs_approval(st: core.NsState, key: str) -> bool:
    pre = approval_prefixes(st)
    if not pre:
        return False
    return any(p == "*" or key.startswith(p) for p in pre)


def approval_id(key: str) -> str:
    return "ap-" + core.sha256_text(key)


def read_approval(c, ns: str, key: str) -> dict | None:
    if not sqldb.table_exists(c, "kh_record"):
        return None
    r = c.execute("SELECT body_json, rev, updated_ms FROM kh_record WHERE ns=? AND type=? AND id=?",
                  (ns, APPROVAL_TYPE, approval_id(key))).fetchone()
    if r is None:
        return None
    b = core.loads(r[0], {}) or {}
    return {"decision": b.get("decision"), "approver": b.get("approver"), "note": b.get("note"),
            "rev": int(r[1]), "at_ms": r[2]}


def approval_valid(st: core.NsState, appr: dict | None) -> bool:
    return bool(appr) and appr.get("decision") == "approve" and (appr.get("approver") or "") in approvers(st)


def _confirmer_resolver(request, row: dict):
    """Persons listed as approvers (effects/items confirm_policy) get role confirmer (not admins)."""
    from app import auth

    p = auth.current()
    if p is None or p.ns or core.is_ns_admin_person(p, row):
        return None
    st = core.ns_state(row["ns"])
    if st is None:
        return None
    emails = set(approvers(st))
    for sec in ("items", "rules", "packs"):
        emails |= {str(e).lower() for e in ((st.settings(sec).get("confirm_policy") or {}).get("emails") or [])}
    for t in st.types.values():
        emails |= {str(e).lower() for e in ((t.get("confirm_policy") or {}).get("emails") or [])}
    if (p.email or "").lower() in emails:
        return core.Caller(p.email, "confirmer", "person", None, core.person_projects(p), p.email)
    return None


# ---------------------------------------------------------------------------
# Leases
# ---------------------------------------------------------------------------


def _lease_view(ns: str, name: str, row: dict | None, now: int) -> dict:
    row = row or {"holder": None, "epoch": 0, "until_ms": 0}
    held = row["holder"] is not None and int(row["until_ms"]) >= now
    return {"ns": ns, "name": name, "holder": row["holder"], "epoch": int(row["epoch"]),
            "until_ms": int(row["until_ms"]), "held": held}


def _check_name(name: str) -> str:
    if not name or len(name) > NAME_MAX:
        raise CapError("invalid", f"lease name must be 1-{NAME_MAX} characters")
    return name


def lease_acquire(ctx: Ctx, name: str, ttl_s: int) -> dict:
    with sqldb.connect() as c:
        got = sqldb.lease_acquire(c, ctx.ns, name, ctx.principal, ttl_s * 1000)
        if got is None:
            cur = sqldb.lease_get(c, ctx.ns, name) or {}
            raise CapError("lease_held", f"lease '{name}' is held by another principal",
                           extra={"holder": cur.get("holder"), "until_ms": cur.get("until_ms")})
    return {"name": name, "epoch": got["epoch"], "until_ms": got["until_ms"]}


def lease_renew(ctx: Ctx, name: str, epoch: int, ttl_s: int) -> dict:
    with sqldb.connect() as c:
        ok = sqldb.lease_renew(c, ctx.ns, name, ctx.principal, epoch, ttl_s * 1000)
        cur = sqldb.lease_get(c, ctx.ns, name) or {}
    if not ok:
        raise CapError("lease_lost", f"lease '{name}' at epoch {epoch} is not held by you",
                       extra={"holder": cur.get("holder"), "epoch": cur.get("epoch"), "until_ms": cur.get("until_ms")},
                       kh_events=[(ctx.ns, "kh.lease_lost", {"lease": name, "epoch": epoch,
                                                             "principal": ctx.principal})])
    return {"name": name, "epoch": int(cur["epoch"]), "until_ms": int(cur["until_ms"])}


def lease_release(ctx: Ctx, name: str, epoch: int | None) -> dict:
    with sqldb.connect() as c:
        ok = sqldb.lease_release(c, ctx.ns, name, ctx.principal, epoch)
    return {"name": name, "released": bool(ok)}


def lease_list(ctx: Ctx, prefix: str, limit: int) -> list[dict]:
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        q = "SELECT name, holder, epoch, until_ms FROM kh_lease WHERE ns=?"
        params: list = [ctx.ns]
        if prefix:
            q += " AND SUBSTR(name, 1, ?)=?"
            params += [len(prefix), prefix]
        q += f" ORDER BY name LIMIT {int(limit)}"
        return [_lease_view(ctx.ns, r[0], {"holder": r[1], "epoch": r[2], "until_ms": r[3]}, now)
                for r in c.execute(q, params).fetchall()]


def lease_get(ctx: Ctx, name: str) -> dict:
    with sqldb.connect() as c:
        row = sqldb.lease_get(c, ctx.ns, name)
        now = sqldb.now_ms(c)
    if row is None:
        raise CapError("not_found", f"lease '{name}' not found")
    return _lease_view(ctx.ns, name, row, now)


# ---------------------------------------------------------------------------
# Effects
# ---------------------------------------------------------------------------


def _effect_view(r, now: int) -> dict:
    res = core.loads(r["result_json"], None)
    until = r["claimed_until_ms"]
    return {"key": r["effect_key"], "holder": r["holder"], "state": r["state"], "result": res,
            "claimed_until_ms": until, "attempts": int(r["attempts"] or 0), "rev": int(r["rev"] or 0),
            "expired": r["state"] == "claimed" and until is not None and int(until) < now}


def _read_effect(c, ns: str, key: str):
    return c.execute("SELECT * FROM kh_effect WHERE ns=? AND effect_key=?", (ns, key)).fetchone()


def _with_approval(c, ctx: Ctx, key: str, view: dict) -> dict:
    if needs_approval(ctx.st, key):
        appr = read_approval(c, ctx.ns, key)
        view["approval_required"] = True
        view["approval"] = appr
    return view


def effect_claim(ctx: Ctx, key: str, ttl_s: int) -> tuple[int, dict]:
    with sqldb.connect() as c:
        if needs_approval(ctx.st, key):
            appr = read_approval(c, ctx.ns, key)
            if not approval_valid(ctx.st, appr):
                raise CapError("approval_required", f"effect '{key}' needs a human approval first",
                               extra={"key": key, "approvers": approvers(ctx.st), "approval": appr})
        now = sqldb.now_ms(c)
        until = now + ttl_s * 1000
        row = {"ns": ctx.ns, "effect_key": key, "holder": ctx.principal, "state": "claimed", "result_json": None,
               "claimed_until_ms": until, "attempts": 1, "rev": 1, "created_ms": now, "updated_ms": now}
        if sqldb.insert_new(c, "kh_effect", row):
            return 201, _with_approval(c, ctx, key, _effect_view(_read_effect(c, ctx.ns, key), now))
        r = _read_effect(c, ctx.ns, key)
        reclaim = r["state"] == "failed" or (r["state"] == "claimed" and int(r["claimed_until_ms"] or 0) < now)
        if reclaim:
            cur = c.execute("UPDATE kh_effect SET holder=?, state='claimed', claimed_until_ms=?, "
                            "attempts=attempts+1, rev=rev+1, updated_ms=? WHERE ns=? AND effect_key=? AND rev=?",
                            (ctx.principal, until, now, ctx.ns, key, int(r["rev"])))
            if cur.rowcount and cur.rowcount > 0:
                return 201, _with_approval(c, ctx, key, _effect_view(_read_effect(c, ctx.ns, key), now))
            r = _read_effect(c, ctx.ns, key)
        raise CapError("effect_claimed", f"effect '{key}' is {r['state']} by {r['holder']}",
                       extra={"effect": _effect_view(r, now)})


def effect_finish(ctx: Ctx, key: str, state: str, result) -> dict:
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        cur = c.execute("UPDATE kh_effect SET state=?, result_json=?, rev=rev+1, updated_ms=? "
                        "WHERE ns=? AND effect_key=? AND holder=? AND state='claimed'",
                        (state, core.dumps(result), now, ctx.ns, key, ctx.principal))
        r = _read_effect(c, ctx.ns, key)
        if r is None:
            raise CapError("not_found", f"effect '{key}' not found")
        if not cur.rowcount or cur.rowcount <= 0:
            raise CapError("effect_claimed", f"effect '{key}' is not claimed by you",
                           extra={"effect": _effect_view(r, now)})
        return _effect_view(r, now)


def effect_get(ctx: Ctx, key: str) -> dict:
    with sqldb.connect() as c:
        r = _read_effect(c, ctx.ns, key)
        now = sqldb.now_ms(c)
        if r is None:
            if needs_approval(ctx.st, key):
                appr = read_approval(c, ctx.ns, key)
                if appr is not None:
                    return {"key": key, "holder": None, "state": None, "result": None, "claimed_until_ms": None,
                            "attempts": 0, "rev": 0, "expired": False, "approval_required": True,
                            "approval": appr}
            raise CapError("not_found", f"effect '{key}' not found")
        return _with_approval(c, ctx, key, _effect_view(r, now))


def effect_approve(ctx: Ctx, key: str, decision: str, note: str | None) -> dict:
    if decision not in ("approve", "reject"):
        raise CapError("invalid", "decision must be approve or reject")
    if note is not None and (not isinstance(note, str) or len(note) > NOTE_MAX):
        raise CapError("invalid", f"note must be a string of at most {NOTE_MAX} characters")
    email = (ctx.caller.email or "").lower()
    if ctx.caller.kind == "local":
        email = email or "local"
    elif ctx.caller.kind != "person" or email not in approvers(ctx.st):
        raise CapError("forbidden", "only a signed-in person listed in settings.effects.confirm_policy.emails "
                                    "may approve effects")
    body = {"key": key, "decision": decision, "approver": email, "note": note}
    with sqldb.connect() as c:
        if not sqldb.table_exists(c, "kh_record"):
            raise CapError("not_found", "approvals are stored as records; the records capability is off")
        now = sqldb.now_ms(c)
        rid = approval_id(key)
        r = c.execute("SELECT rev FROM kh_record WHERE ns=? AND type=? AND id=?",
                      (ctx.ns, APPROVAL_TYPE, rid)).fetchone()
        rev = int(r[0]) + 1 if r else 1
        row = {"ns": ctx.ns, "type": APPROVAL_TYPE, "id": rid, "project": "", "schema_ver": None, "rev": rev,
               "state": "open", "ix_s1": key[:core.IX_STR_MAX],
               "ix_s2": email, "ix_s3": decision, "body_json": core.dumps(body), "actor_json": ctx.actor_json(),
               "created_ms": now, "updated_ms": now}
        if r is None:
            sqldb.insert_new(c, "kh_record", row)
        else:
            c.execute("UPDATE kh_record SET rev=?, state=?, ix_s2=?, ix_s3=?, body_json=?, actor_json=?, "
                      "updated_ms=? WHERE ns=? AND type=? AND id=?",
                      (rev, row["state"], email, decision, row["body_json"], row["actor_json"], now,
                       ctx.ns, APPROVAL_TYPE, rid))
        if sqldb.table_exists(c, "kh_record_rev"):
            sqldb.insert_new(c, "kh_record_rev", {"ns": ctx.ns, "type": APPROVAL_TYPE, "id": rid, "rev": rev,
                                                  "state": row["state"], "schema_ver": None,
                                                  "body_json": row["body_json"], "actor_json": row["actor_json"],
                                                  "at_ms": now})
    core.emit(ctx.ns, "kh.intervention", {"what": "effect_approval", "key": key, "decision": decision,
                                          "approver": email}, ctx)
    return {"key": key, "approval": {**body, "rev": rev, "at_ms": now}}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _ttl(body: dict, lo: int, hi: int, default: int) -> int:
    return core.parse_int(body.get("ttl_s"), "ttl_s", default, lo, hi)


def _epoch(body: dict, ctx: Ctx, required: bool) -> int | None:
    e = core.parse_int(body.get("epoch"), "epoch")
    if e is None:
        e = ctx.epoch
    if e is None and required:
        raise CapError("invalid", "epoch is required (body or X-KH-Epoch)")
    return e


def attach(app: FastAPI) -> None:
    tag = ["v1 coord"]

    @app.get("/v1/{ns}/leases", tags=tag, summary="List leases")
    async def get_leases(ns: str, prefix: str = Query(""), limit: str | None = Query(None),
                         ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require("leases", "use")
        lim = core.clamp_limit(limit, 100, LIST_MAX)
        return {"leases": await core.run(lease_list, ctx, prefix or "", lim)}

    @app.post("/v1/{ns}/leases/{name:path}", tags=tag, summary="Lease verbs: name:acquire | :renew | :release")
    @core.idempotent
    async def post_lease(request: Request, ns: str, name: str, ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require("leases", "use")
        lname, verb = core.split_verb(name, LEASE_VERBS)
        if verb is None:
            raise CapError("not_found", "use leases/{name}:acquire, :renew or :release")
        _check_name(lname)
        body = await core.read_object(request)
        if verb == "acquire":
            out = await core.run(lease_acquire, ctx, lname, _ttl(body, LEASE_TTL_MIN, LEASE_TTL_MAX,
                                                                 LEASE_TTL_DEFAULT))
        elif verb == "renew":
            out = await core.run(lease_renew, ctx, lname, _epoch(body, ctx, True),
                                 _ttl(body, LEASE_TTL_MIN, LEASE_TTL_MAX, LEASE_TTL_DEFAULT))
        else:
            out = await core.run(lease_release, ctx, lname, _epoch(body, ctx, False))
        return core.reply(out)

    @app.get("/v1/{ns}/leases/{name:path}", tags=tag, summary="Read one lease")
    async def get_lease(ns: str, name: str, ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require("leases", "use")
        return await core.run(lease_get, ctx, _check_name(name))

    @app.post("/v1/{ns}/effects/{key:path}", tags=tag,
              summary="Effect verbs: key:claim | :complete | :fail | :approve")
    @core.idempotent
    async def post_effect(request: Request, ns: str, key: str, ctx: Ctx = Depends(core.ns_ctx)):
        ekey, verb = core.split_verb(key, EFFECT_VERBS)
        if verb is None:
            raise CapError("not_found", "use effects/{key}:claim, :complete, :fail or :approve")
        _check_name(ekey)
        body = await core.read_object(request)
        if verb == "approve":
            out = await core.run(effect_approve, ctx, ekey, body.get("decision"), body.get("note"))
            return core.reply(out)
        ctx.require("effects", "use")
        if verb == "claim":
            status, out = await core.run(effect_claim, ctx, ekey, _ttl(body, EFFECT_TTL_MIN, EFFECT_TTL_MAX,
                                                                         EFFECT_TTL_DEFAULT))
            return core.reply(out, status=status, etag_rev=out["rev"])
        if verb == "complete":
            out = await core.run(effect_finish, ctx, ekey, "done", body.get("result"))
        else:
            out = await core.run(effect_finish, ctx, ekey, "failed", {"error": body.get("error")})
        return core.reply(out, etag_rev=out["rev"])

    @app.get("/v1/{ns}/effects/{key:path}", tags=tag, summary="Read one effect")
    async def get_effect(ns: str, key: str, ctx: Ctx = Depends(core.ns_ctx)):
        if not (ctx.allows("effects", "use") or ctx.role == "confirmer"):
            ctx.require("effects", "use")
        out = await core.run(effect_get, ctx, _check_name(key))
        return core.reply(out, etag_rev=out["rev"] or None)
