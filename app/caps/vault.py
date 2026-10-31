"""vault (spec §6.11): sealed holdout items readable only by graders or the ns admin, with canaries.

A vault exists only when the manifest declares it: `vaults: [{vault_id, refresh_days=30, retire_days=180,
fenced=false, project?}]`. Nothing here is embedded, projected or exposed to records/rules/retrieval; events carry
ids only. Every item carries 1..5 KH canaries (`khc-` + 16 hex) and up to 20 custom canary strings. `scan(ns, text)`
finds canaries in any other write (registered as a core BODY_FILTER, so events/records/... writes that leak a
canary are stored with `[REDACTED:canary]` and flagged with `kh.canary_hit`; never refused, D10).
"""
from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

from fastapi import Depends, FastAPI, Query, Request

from app import sqldb
from app.caps import core
from app.caps.core import CapError, Ctx

NAME = "vault"
VERSION = "1.0.0"
STATUS = "beta"
DEFAULT_ON = True

KH_CANARIES_MIN, KH_CANARIES_MAX, KH_CANARIES_DEFAULT = 1, 5, 1
CUSTOM_MAX = 20
CUSTOM_MIN_CHARS, CUSTOM_MAX_CHARS = 12, 256
DAYS_MIN, DAYS_MAX = 1, 3650
REFRESH_DAYS_DEFAULT = 30
RETIRE_DAYS_DEFAULT = 180
LIST_DEFAULT, LIST_MAX = 100, 500
BODY_MAX = 262_144            # bytes of one item body (UNKNOWN in spec)
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
KH_CANARY_RE = re.compile(r"khc-[0-9a-f]{16}")
CACHE_S = 5.0
DAY_MS = 86_400_000
MASK = "[REDACTED:canary]"
LIMITS = {"kh_canaries": [KH_CANARIES_MIN, KH_CANARIES_MAX], "kh_canaries_default": KH_CANARIES_DEFAULT,
          "custom_canaries": CUSTOM_MAX, "custom_canary_chars": [CUSTOM_MIN_CHARS, CUSTOM_MAX_CHARS],
          "refresh_days": [DAYS_MIN, DAYS_MAX], "refresh_days_default": REFRESH_DAYS_DEFAULT,
          "retire_days_default": RETIRE_DAYS_DEFAULT, "list_max": LIST_MAX, "body_bytes": BODY_MAX}
TABLES = ["kh_vault", "kh_vault_item", "kh_vault_canary"]
LOCAL_TABLES: list[str] = []
VAULT_VERBS = ("refresh",)


def _m1(c) -> None:
    sqldb.create_table(
        c, "kh_vault",
        "CREATE TABLE IF NOT EXISTS kh_vault (ns TEXT NOT NULL, vault_id TEXT NOT NULL, refresh_days INTEGER, "
        "created_ms INTEGER, last_refresh_ms INTEGER, next_due_ms INTEGER, refresh_count INTEGER NOT NULL DEFAULT 0, "
        "wseq INTEGER NOT NULL DEFAULT 0, rev INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (ns, vault_id))",
        "CREATE COLUMN TABLE kh_vault (ns NVARCHAR(64) NOT NULL, vault_id NVARCHAR(128) NOT NULL, "
        "refresh_days INTEGER, created_ms BIGINT, last_refresh_ms BIGINT, next_due_ms BIGINT, "
        "refresh_count BIGINT NOT NULL, wseq BIGINT NOT NULL, rev BIGINT NOT NULL, PRIMARY KEY (ns, vault_id))")
    sqldb.create_table(
        c, "kh_vault_item",
        "CREATE TABLE IF NOT EXISTS kh_vault_item (ns TEXT NOT NULL, vault_id TEXT NOT NULL, item_id TEXT NOT NULL, "
        "project TEXT, version INTEGER NOT NULL DEFAULT 1, body_json TEXT, canaries_json TEXT, actor_json TEXT, "
        "created_ms INTEGER, updated_ms INTEGER, refreshed_ms INTEGER, rev INTEGER NOT NULL DEFAULT 1, "
        "PRIMARY KEY (ns, vault_id, item_id))",
        "CREATE COLUMN TABLE kh_vault_item (ns NVARCHAR(64) NOT NULL, vault_id NVARCHAR(128) NOT NULL, "
        "item_id NVARCHAR(128) NOT NULL, project NVARCHAR(256), version BIGINT NOT NULL, body_json NCLOB, "
        "canaries_json NCLOB, actor_json NCLOB, created_ms BIGINT, updated_ms BIGINT, refreshed_ms BIGINT, "
        "rev BIGINT NOT NULL, PRIMARY KEY (ns, vault_id, item_id))")
    sqldb.create_table(
        c, "kh_vault_canary",
        "CREATE TABLE IF NOT EXISTS kh_vault_canary (ns TEXT NOT NULL, canary TEXT NOT NULL, vault_id TEXT NOT NULL, "
        "item_id TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL, created_ms INTEGER, retired_ms INTEGER, "
        "expires_ms INTEGER, PRIMARY KEY (ns, canary))",
        "CREATE COLUMN TABLE kh_vault_canary (ns NVARCHAR(64) NOT NULL, canary NVARCHAR(256) NOT NULL, "
        "vault_id NVARCHAR(128) NOT NULL, item_id NVARCHAR(128) NOT NULL, kind NVARCHAR(8) NOT NULL, "
        "state NVARCHAR(16) NOT NULL, created_ms BIGINT, retired_ms BIGINT, expires_ms BIGINT, "
        "PRIMARY KEY (ns, canary))")
    sqldb.create_index(c, "kh_vault_canary_item", "kh_vault_canary", ["ns", "vault_id", "item_id"])
    sqldb.create_index(c, "kh_vault_due", "kh_vault", ["next_due_ms"])


def init(c) -> None:
    sqldb.migrate(c, NAME, [(1, _m1)])
    from app import caps

    red = caps.module("redaction")
    if red is not None and hasattr(red, "register_canary"):
        red.register_canary(scan)            # redaction masks + emits kh.canary_hit on every write path
        if body_filter in core.BODY_FILTERS:
            core.BODY_FILTERS.remove(body_filter)
    elif body_filter not in core.BODY_FILTERS:
        core.BODY_FILTERS.append(body_filter)  # redaction off: vault scans on its own


# ---------------------------------------------------------------------------
# Declarations
# ---------------------------------------------------------------------------


def _days(v: Any, default: int) -> int:
    try:
        d = int(v)
    except (TypeError, ValueError):
        return default
    return d if DAYS_MIN <= d <= DAYS_MAX else default


def declared(st: core.NsState) -> dict[str, dict]:
    """vault_id -> {vault_id, refresh_days, retire_days, fenced, project} from the manifest."""
    out: dict[str, dict] = {}
    for v in st.config.get("vaults") or []:
        if not isinstance(v, dict) or not isinstance(v.get("vault_id"), str) or not v.get("vault_id"):
            continue
        out[v["vault_id"]] = {"vault_id": v["vault_id"],
                              "refresh_days": _days(v.get("refresh_days"), REFRESH_DAYS_DEFAULT),
                              "retire_days": _days(v.get("retire_days"), RETIRE_DAYS_DEFAULT),
                              "fenced": bool(v.get("fenced", False)),
                              "project": (v.get("project") or "") if isinstance(v.get("project"), str) else ""}
    return out


def _require(ctx: Ctx) -> None:
    """Graders (vault:read) and the ns admin only; every other role 403 before any lookup."""
    if not (ctx.is_admin or ctx.allows("vault", "read")):
        raise CapError("forbidden", f"role '{ctx.role}' may not use the vault", extra={"area": "vault",
                                                                                      "action": "read"})


def _decl(ctx: Ctx, vault_id: str) -> dict:
    d = declared(ctx.st).get(vault_id)
    if d is None:
        raise CapError("vault_not_declared", f"vault '{vault_id}' is not declared in the manifest")
    return d


def _ensure_vault(c, ns: str, d: dict, now: int) -> dict:
    sqldb.insert_new(c, "kh_vault", {"ns": ns, "vault_id": d["vault_id"], "refresh_days": d["refresh_days"],
                                     "created_ms": now, "last_refresh_ms": None,
                                     "next_due_ms": now + d["refresh_days"] * DAY_MS, "refresh_count": 0,
                                     "wseq": 0, "rev": 1})
    r = c.execute("SELECT * FROM kh_vault WHERE ns=? AND vault_id=?", (ns, d["vault_id"])).fetchone()
    if r["refresh_days"] != d["refresh_days"]:  # manifest changed the period: re-plan from the last rotation
        base = r["last_refresh_ms"] or r["created_ms"] or now
        c.execute("UPDATE kh_vault SET refresh_days=?, next_due_ms=? WHERE ns=? AND vault_id=?",
                  (d["refresh_days"], int(base) + d["refresh_days"] * DAY_MS, ns, d["vault_id"]))
        r = c.execute("SELECT * FROM kh_vault WHERE ns=? AND vault_id=?", (ns, d["vault_id"])).fetchone()
    return dict(r)


# ---------------------------------------------------------------------------
# Canary cache and scan
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, dict, re.Pattern | None]] = {}
_cache_lock = threading.Lock()


def invalidate(ns: str | None = None) -> None:
    with _cache_lock:
        if ns is None:
            _cache.clear()
        else:
            _cache.pop(ns, None)


def _load(ns: str) -> tuple[dict, re.Pattern | None]:
    now_t = time.monotonic()
    with _cache_lock:
        hit = _cache.get(ns)
        if hit and now_t - hit[0] < CACHE_S:
            return hit[1], hit[2]
    with sqldb.connect() as c:
        if not sqldb.table_exists(c, "kh_vault_canary"):
            return {}, None
        now = sqldb.now_ms(c)
        rows = c.execute("SELECT canary, vault_id, item_id, kind, state FROM kh_vault_canary WHERE ns=? AND "
                         "(state='active' OR expires_ms > ?)", (ns, now)).fetchall()
    known = {r[0]: {"vault": r[1], "item": r[2], "kind": r[3], "retired": r[4] != "active"} for r in rows}
    custom = sorted((k for k, v in known.items() if v["kind"] == "custom"), key=len, reverse=True)
    rx = re.compile("|".join(re.escape(s) for s in custom)) if custom else None
    with _cache_lock:
        _cache[ns] = (now_t, known, rx)
    return known, rx


def _lookup_db(ns: str, canary: str) -> dict | None:
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        r = c.execute("SELECT vault_id, item_id, kind, state FROM kh_vault_canary WHERE ns=? AND canary=? AND "
                      "(state='active' OR expires_ms > ?)", (ns, canary, now)).fetchone()
    if r is None:
        return None
    return {"vault": r[0], "item": r[1], "kind": r[2], "retired": r[3] != "active"}


def scan(ns: str, text: str) -> list[dict]:
    """Canary hits in `text`: [{start, end, canary, vault, item, canary_kind, retired}] (sorted, non-overlapping)."""
    if not isinstance(text, str) or not text:
        return []
    known, rx = _load(ns)
    hits: list[dict] = []
    for m in KH_CANARY_RE.finditer(text):
        info = known.get(m.group(0)) or _lookup_db(ns, m.group(0))
        if info:
            hits.append({"start": m.start(), "end": m.end(), "canary": m.group(0), "vault": info["vault"],
                         "item": info["item"], "canary_kind": info["kind"], "retired": info["retired"]})
    if rx is not None:
        for m in rx.finditer(text):
            info = known.get(m.group(0))
            if info:
                hits.append({"start": m.start(), "end": m.end(), "canary": m.group(0), "vault": info["vault"],
                             "item": info["item"], "canary_kind": info["kind"], "retired": info["retired"]})
    hits.sort(key=lambda h: (h["start"], -(h["end"] - h["start"])))
    out, end = [], -1
    for h in hits:
        if h["start"] >= end:
            out.append(h)
            end = h["end"]
    return out


def mask_text(ns: str, text: str) -> tuple[str, list[dict]]:
    hits = scan(ns, text)
    if not hits:
        return text, []
    parts, pos = [], 0
    for h in hits:
        parts.append(text[pos:h["start"]])
        parts.append(MASK)
        pos = h["end"]
    parts.append(text[pos:])
    return "".join(parts), hits


def _ptr(token: Any) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def _walk(ns: str, value: Any, pointer: str, found: list) -> Any:
    if isinstance(value, str):
        masked, hits = mask_text(ns, value)
        for h in hits:
            found.append((pointer, h))
        return masked
    if isinstance(value, list):
        return [_walk(ns, v, f"{pointer}/{i}", found) for i, v in enumerate(value)]
    if isinstance(value, dict):
        return {k: _walk(ns, v, f"{pointer}/{_ptr(k)}", found) for k, v in value.items()}
    return value


def body_filter(ctx: Ctx | None, where: str, body: Any) -> Any:
    """BODY_FILTER: mask canaries in any /v1 write (vault's own writes are exempt) and flag `kh.canary_hit`."""
    if ctx is None or (where or "").startswith("vault"):
        return body
    found: list = []
    out = _walk(ctx.ns, body, "", found)
    for pointer, h in found:
        core.emit(ctx.ns, "kh.canary_hit", {"vault": h["vault"], "item": h["item"], "canary_kind": h["canary_kind"],
                                            "retired": h["retired"], "principal": ctx.principal,
                                            "run_id": ctx.run_id, "unit_id": ctx.unit_id, "where": where,
                                            "pointer": pointer or "/"}, ctx)
    return out


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


def _mint() -> str:
    return "khc-" + os.urandom(8).hex()


def _active_canaries(c, ns: str, vault_id: str, item_id: str) -> list[dict]:
    rows = c.execute("SELECT canary, kind FROM kh_vault_canary WHERE ns=? AND vault_id=? AND item_id=? AND "
                     "state='active' ORDER BY kind, created_ms, canary", (ns, vault_id, item_id)).fetchall()
    return [{"canary": r[0], "kind": r[1]} for r in rows]


def _retire(c, ns: str, where: str, params: list, now: int, retire_days: int) -> int:
    cur = c.execute(f"UPDATE kh_vault_canary SET state='retired', retired_ms=?, expires_ms=? WHERE ns=? AND "
                    f"state='active' AND {where}", [now, now + retire_days * DAY_MS, ns] + params)
    return int(cur.rowcount or 0)


def _insert_canary(c, ns: str, canary: str, vault_id: str, item_id: str, kind: str, now: int) -> bool:
    return sqldb.insert_new(c, "kh_vault_canary", {"ns": ns, "canary": canary, "vault_id": vault_id,
                                                   "item_id": item_id, "kind": kind, "state": "active",
                                                   "created_ms": now, "retired_ms": None, "expires_ms": None})


def _mint_kh(c, ns: str, vault_id: str, item_id: str, n: int, now: int) -> None:
    made = 0
    while made < n:
        if _insert_canary(c, ns, _mint(), vault_id, item_id, "kh", now):
            made += 1


def _item_view(r, canaries: list[dict] | None, with_body: bool) -> dict:
    out = {"vault": r["vault_id"], "item_id": r["item_id"], "project": r["project"] or "",
           "version": int(r["version"]), "rev": int(r["rev"]), "created_ms": r["created_ms"],
           "updated_ms": r["updated_ms"], "refreshed_ms": r["refreshed_ms"]}
    if with_body:
        out["body"] = core.loads(r["body_json"], None)
    if canaries is not None:
        out["canaries"] = [x["canary"] for x in canaries]
        out["kh_canaries"] = sum(1 for x in canaries if x["kind"] == "kh")
        out["custom_canaries"] = sum(1 for x in canaries if x["kind"] == "custom")
    return out


def _check_custom(v: Any) -> list[str] | None:
    if v is None:
        return None
    if not isinstance(v, list) or len(v) > CUSTOM_MAX:
        raise CapError("invalid", f"canaries must be a list of at most {CUSTOM_MAX} strings")
    out: list[str] = []
    for s in v:
        if not isinstance(s, str) or not CUSTOM_MIN_CHARS <= len(s) <= CUSTOM_MAX_CHARS:
            raise CapError("invalid", f"each canary must be a string of {CUSTOM_MIN_CHARS}-{CUSTOM_MAX_CHARS} "
                                      "characters")
        if KH_CANARY_RE.search(s):
            raise CapError("invalid", "custom canaries may not use the KH canary format (khc-...)")
        if s not in out:
            out.append(s)
    return out


def put_item(ctx: Ctx, d: dict, item_id: str, body: dict, if_match: int | None, if_none_match: bool) -> tuple[int, dict]:
    custom = _check_custom(body.get("canaries"))
    n_kh = core.parse_int(body.get("kh_canaries"), "kh_canaries", None, KH_CANARIES_MIN, KH_CANARIES_MAX)
    project = body.get("project")
    if project is not None and not isinstance(project, str):
        raise CapError("invalid", "project must be a string")
    project = project or d["project"]
    if project:
        project = ctx.require_project(project)
    payload = body["body"]
    body_sha = core.sha256_json(payload)
    vid = d["vault_id"]
    with sqldb.connect() as c:
        core.fence(ctx, c, required=d["fenced"])
        now = sqldb.now_ms(c)
        _ensure_vault(c, ctx.ns, d, now)
        c.execute("UPDATE kh_vault SET wseq=wseq+1 WHERE ns=? AND vault_id=?", (ctx.ns, vid))  # serialize writers
        r = c.execute("SELECT * FROM kh_vault_item WHERE ns=? AND vault_id=? AND item_id=?",
                      (ctx.ns, vid, item_id)).fetchone()
        if r is not None and if_none_match:
            raise core.write_conflict(ctx.ns, f"vault item '{item_id}'", int(r["rev"]), None)
        if r is None and if_match is not None and if_match >= 0:
            raise CapError("not_found", f"vault item '{item_id}' not found")
        if r is not None and if_match is not None and if_match >= 0 and int(r["rev"]) != if_match:
            raise core.write_conflict(ctx.ns, f"vault item '{item_id}'", int(r["rev"]), if_match)
        if custom:  # unique per ns (any state) unless it is already this item's
            for s in custom:
                o = c.execute("SELECT vault_id, item_id FROM kh_vault_canary WHERE ns=? AND canary=?",
                              (ctx.ns, s)).fetchone()
                if o is not None and (o[0], o[1]) != (vid, item_id):
                    raise CapError("canary_conflict", "a custom canary is already used by another vault item",
                                   extra={"vault": o[0], "item": o[1]})
        if r is None:
            row = {"ns": ctx.ns, "vault_id": vid, "item_id": item_id, "project": project,
                   "version": 1, "body_json": core.dumps(payload), "canaries_json": None,
                   "actor_json": ctx.actor_json(), "created_ms": now, "updated_ms": now, "refreshed_ms": None,
                   "rev": 1}
            if not sqldb.insert_new(c, "kh_vault_item", row):
                raise core.write_conflict(ctx.ns, f"vault item '{item_id}'", None, if_match)
            status = 201
            _mint_kh(c, ctx.ns, vid, item_id, n_kh or KH_CANARIES_DEFAULT, now)
        else:
            status = 200
            changed = core.sha256_json(core.loads(r["body_json"], None)) != body_sha
            c.execute("UPDATE kh_vault_item SET project=?, version=?, body_json=?, actor_json=?, updated_ms=?, "
                      "rev=rev+1 WHERE ns=? AND vault_id=? AND item_id=?",
                      (project, int(r["version"]) + (1 if changed else 0), core.dumps(payload), ctx.actor_json(),
                       now, ctx.ns, vid, item_id))
            if n_kh is not None:
                kh = [x["canary"] for x in _active_canaries(c, ctx.ns, vid, item_id) if x["kind"] == "kh"]
                if len(kh) < n_kh:
                    _mint_kh(c, ctx.ns, vid, item_id, n_kh - len(kh), now)
                elif len(kh) > n_kh:
                    extra = kh[n_kh:]
                    _retire(c, ctx.ns, f"canary IN ({','.join('?' for _ in extra)})", extra, now, d["retire_days"])
        if custom is not None:
            cur = [x["canary"] for x in _active_canaries(c, ctx.ns, vid, item_id) if x["kind"] == "custom"]
            gone = [s for s in cur if s not in custom]
            if gone:
                _retire(c, ctx.ns, f"canary IN ({','.join('?' for _ in gone)})", gone, now, d["retire_days"])
            for s in custom:
                if not _insert_canary(c, ctx.ns, s, vid, item_id, "custom", now):
                    c.execute("UPDATE kh_vault_canary SET state='active', retired_ms=NULL, expires_ms=NULL "
                              "WHERE ns=? AND canary=?", (ctx.ns, s))
        cans = _active_canaries(c, ctx.ns, vid, item_id)
        c.execute("UPDATE kh_vault_item SET canaries_json=? WHERE ns=? AND vault_id=? AND item_id=?",
                  (core.dumps([x["canary"] for x in cans]), ctx.ns, vid, item_id))
        r = c.execute("SELECT * FROM kh_vault_item WHERE ns=? AND vault_id=? AND item_id=?",
                      (ctx.ns, vid, item_id)).fetchone()
        view = _item_view(r, cans, with_body=False)
    invalidate(ctx.ns)
    return status, view


def read_items(ctx: Ctx, d: dict, ids: list[str] | None, after: str, limit: int) -> dict:
    vid = d["vault_id"]
    with sqldb.connect() as c:
        if ids is not None:
            rows = c.execute(f"SELECT * FROM kh_vault_item WHERE ns=? AND vault_id=? AND item_id IN "
                             f"({','.join('?' for _ in ids)}) ORDER BY item_id", [ctx.ns, vid] + ids).fetchall()
        else:
            rows = c.execute(f"SELECT * FROM kh_vault_item WHERE ns=? AND vault_id=? AND item_id > ? "
                             f"ORDER BY item_id LIMIT {int(limit) + 1}", (ctx.ns, vid, after or "")).fetchall()
        more = ids is None and len(rows) > limit
        rows = [r for r in rows[:limit] if ctx.may_project(r["project"])]
        items = [_item_view(r, _active_canaries(c, ctx.ns, vid, r["item_id"]), with_body=True) for r in rows]
    out: dict = {"vault": vid, "items": items}
    if more and items:
        out["next"] = items[-1]["item_id"]
    elif more:
        out["next"] = None
    return out


def _read_event(ctx: Ctx, vid: str, items: list[dict]) -> None:
    if items:
        core.emit(ctx.ns, "kh.vault_read", {"vault": vid, "count": len(items),
                                             "item_ids": [i["item_id"] for i in items]}, ctx)


def delete_item(ctx: Ctx, d: dict, item_id: str, if_match: int | None) -> dict:
    vid = d["vault_id"]
    with sqldb.connect() as c:
        core.fence(ctx, c, required=d["fenced"])
        now = sqldb.now_ms(c)
        _ensure_vault(c, ctx.ns, d, now)
        c.execute("UPDATE kh_vault SET wseq=wseq+1 WHERE ns=? AND vault_id=?", (ctx.ns, vid))
        r = c.execute("SELECT rev, project FROM kh_vault_item WHERE ns=? AND vault_id=? AND item_id=?",
                      (ctx.ns, vid, item_id)).fetchone()
        if r is None or not ctx.may_project(r[1]):
            raise CapError("not_found", f"vault item '{item_id}' not found")
        if if_match is not None and if_match >= 0 and int(r[0]) != if_match:
            raise core.write_conflict(ctx.ns, f"vault item '{item_id}'", int(r[0]), if_match)
        c.execute("DELETE FROM kh_vault_item WHERE ns=? AND vault_id=? AND item_id=?", (ctx.ns, vid, item_id))
        n = _retire(c, ctx.ns, "vault_id=? AND item_id=?", [vid, item_id], now, d["retire_days"])
    invalidate(ctx.ns)
    return {"vault": vid, "deleted": item_id, "retired_canaries": n}


# ---------------------------------------------------------------------------
# Status and rotation
# ---------------------------------------------------------------------------


def _status(c, ns: str, d: dict, now: int) -> dict:
    v = _ensure_vault(c, ns, d, now)
    vid = d["vault_id"]
    items = int(c.execute("SELECT COUNT(*) FROM kh_vault_item WHERE ns=? AND vault_id=?", (ns, vid)).fetchone()[0])
    counts = {"kh": 0, "custom": 0, "retired": 0}
    for r in c.execute("SELECT kind, state, COUNT(*) FROM kh_vault_canary WHERE ns=? AND vault_id=? "
                       "GROUP BY kind, state", (ns, vid)).fetchall():
        if r[1] == "active":
            counts[r[0]] = counts.get(r[0], 0) + int(r[2])
        else:
            counts["retired"] += int(r[2])
    nd = v["next_due_ms"]
    return {"vault_id": vid, "refresh_days": d["refresh_days"], "retire_days": d["retire_days"],
            "fenced": d["fenced"], "project": d["project"], "items": items, "created_ms": v["created_ms"],
            "last_refresh_ms": v["last_refresh_ms"], "next_due_ms": nd,
            "refresh_due": nd is not None and int(nd) <= now, "refresh_count": int(v["refresh_count"] or 0),
            "canaries": {"active_kh": counts["kh"], "active_custom": counts["custom"], "retired": counts["retired"]},
            "rev": int(v["rev"])}


def vault_list(ctx: Ctx) -> dict:
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        return {"vaults": [_status(c, ctx.ns, d, now) for d in declared(ctx.st).values()]}


def vault_get(ctx: Ctx, d: dict) -> dict:
    with sqldb.connect() as c:
        return _status(c, ctx.ns, d, sqldb.now_ms(c))


def _rotate(c, ns: str, d: dict, now: int) -> int:
    """Retire every active KH canary of the vault and mint as many new ones per item. Returns items rotated."""
    vid = d["vault_id"]
    per_item: dict[str, int] = {}
    for r in c.execute("SELECT item_id, COUNT(*) FROM kh_vault_canary WHERE ns=? AND vault_id=? AND kind='kh' AND "
                       "state='active' GROUP BY item_id", (ns, vid)).fetchall():
        per_item[r[0]] = int(r[1])
    items = [r[0] for r in c.execute("SELECT item_id FROM kh_vault_item WHERE ns=? AND vault_id=?",
                                     (ns, vid)).fetchall()]
    _retire(c, ns, "vault_id=? AND kind='kh'", [vid], now, d["retire_days"])
    for item in items:
        _mint_kh(c, ns, vid, item, per_item.get(item) or KH_CANARIES_DEFAULT, now)
        cans = _active_canaries(c, ns, vid, item)
        c.execute("UPDATE kh_vault_item SET refreshed_ms=?, canaries_json=? WHERE ns=? AND vault_id=? AND "
                  "item_id=?", (now, core.dumps([x["canary"] for x in cans]), ns, vid, item))
    return len(items)


def refresh_now(ctx: Ctx, d: dict, expected_rev: int | None) -> tuple[dict, int]:
    vid = d["vault_id"]
    with sqldb.connect() as c:
        core.fence(ctx, c, required=d["fenced"])
        now = sqldb.now_ms(c)
        _ensure_vault(c, ctx.ns, d, now)
        core.cas_update(c, "kh_vault", {"ns": ctx.ns, "vault_id": vid},
                        {"last_refresh_ms": now, "next_due_ms": now + d["refresh_days"] * DAY_MS},
                        expected_rev, ns=ctx.ns, what=f"vault '{vid}'")
        c.execute("UPDATE kh_vault SET wseq=wseq+1, refresh_count=refresh_count+1 WHERE ns=? AND vault_id=?",
                  (ctx.ns, vid))
        n = _rotate(c, ctx.ns, d, now)
        out = _status(c, ctx.ns, d, now)
    invalidate(ctx.ns)
    return out, n


def refresh_due() -> int:
    """Cron `vault.refresh` (hourly): rotate due vaults (one winner per due mark) and drop expired canaries."""
    done = 0
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        due = [(r[0], r[1], r[2]) for r in c.execute(
            "SELECT ns, vault_id, next_due_ms FROM kh_vault WHERE next_due_ms <= ?", (now,)).fetchall()]
    events = []
    for ns, vid, mark in due:
        st = core.ns_state(ns)
        if st is None or st.state != "active":
            continue
        d = declared(st).get(vid)
        if d is None:
            continue
        with sqldb.connect() as c:
            now = sqldb.now_ms(c)
            cur = c.execute("UPDATE kh_vault SET last_refresh_ms=?, next_due_ms=?, refresh_count=refresh_count+1, "
                            "wseq=wseq+1, rev=rev+1 WHERE ns=? AND vault_id=? AND next_due_ms=?",
                            (now, now + d["refresh_days"] * DAY_MS, ns, vid, mark))
            if not cur.rowcount or cur.rowcount <= 0:
                continue
            n = _rotate(c, ns, d, now)
        invalidate(ns)
        done += 1
        events.append((ns, {"vault": vid, "reason": "schedule", "items": n, "refreshed_ms": now,
                            "next_due_ms": now + d["refresh_days"] * DAY_MS}))
    with sqldb.connect() as c:
        c.execute("DELETE FROM kh_vault_canary WHERE state='retired' AND expires_ms IS NOT NULL AND expires_ms < ?",
                  (sqldb.now_ms(c),))
    for ns, body in events:
        core.emit(ns, "vault.refresh_due", body)
    return done


CRONS = [("vault.refresh", 3600, refresh_due)]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _check_item_id(item_id: str) -> str:
    if not ITEM_ID_RE.match(item_id or ""):
        raise CapError("invalid", f"item id must match {ITEM_ID_RE.pattern}")
    return item_id


def attach(app: FastAPI) -> None:
    tag = ["v1 vault"]

    @app.get("/v1/{ns}/vault", tags=tag, summary="Status of the declared vaults")
    async def get_vaults(ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require(ctx)
        return await core.run(vault_list, ctx)

    @app.post("/v1/{ns}/vault/{vault}:refresh", tags=tag, summary="Rotate a vault's canaries now")
    @core.idempotent
    async def post_vault_refresh(request: Request, ns: str, vault: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require(ctx)
        vid = vault
        d = _decl(ctx, vid)
        out, n = await core.run(refresh_now, ctx, d, core.if_match(request))
        await core.run(core.emit, ctx.ns, "vault.refresh_due", {"vault": vid, "reason": "manual", "items": n,
                                                                 "refreshed_ms": out["last_refresh_ms"],
                                                                 "next_due_ms": out["next_due_ms"]}, ctx)
        return core.reply({**out, "rotated_items": n}, etag_rev=out["rev"])

    @app.get("/v1/{ns}/vault/{vault}", tags=tag, summary="One declared vault")
    async def get_vault(ns: str, vault: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require(ctx)
        out = await core.run(vault_get, ctx, _decl(ctx, vault))
        return core.reply(out, etag_rev=out["rev"])

    @app.get("/v1/{ns}/vault/{vault}/items", tags=tag, summary="Read vault items (ids=a,b or after=&limit=)")
    async def get_items(ns: str, vault: str, ids: str | None = Query(None), after: str = Query(""),
                        limit: str | None = Query(None), ctx: Ctx = Depends(core.ns_ctx)):
        _require(ctx)
        d = _decl(ctx, vault)
        lim = core.clamp_limit(limit, LIST_DEFAULT, LIST_MAX)
        id_list = core.csv(ids) if ids is not None else None
        if id_list is not None and len(id_list) > LIST_MAX:
            raise CapError("invalid", f"at most {LIST_MAX} ids")
        out = await core.run(read_items, ctx, d, id_list, after, lim)
        await core.run(_read_event, ctx, d["vault_id"], out["items"])
        return out

    @app.get("/v1/{ns}/vault/{vault}/items/{item_id}", tags=tag, summary="One vault item with body and canaries")
    async def get_item(ns: str, vault: str, item_id: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require(ctx)
        d = _decl(ctx, vault)
        out = await core.run(read_items, ctx, d, [item_id], "", 1)
        if not out["items"]:
            raise CapError("not_found", f"vault item '{item_id}' not found")
        await core.run(_read_event, ctx, d["vault_id"], out["items"])
        item = out["items"][0]
        return core.reply(item, etag_rev=item["rev"])

    @app.put("/v1/{ns}/vault/{vault}/items/{item_id}", tags=tag, summary="Create or replace a vault item")
    async def put_item_route(request: Request, ns: str, vault: str, item_id: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require(ctx)
        d = _decl(ctx, vault)
        _check_item_id(item_id)
        body = await core.read_object(request)
        if "body" not in body or body["body"] is None:
            raise CapError("invalid", "body is required")
        core.check_size(body["body"], BODY_MAX)
        body["body"] = await core.run(core.apply_body_filters, ctx, "vault", body["body"])  # secrets only
        inm = (request.headers.get("if-none-match") or "").strip() == "*"
        status, out = await core.run(put_item, ctx, d, item_id, body, core.if_match(request), inm)
        return core.reply(out, status=status, etag_rev=out["rev"])

    @app.delete("/v1/{ns}/vault/{vault}/items/{item_id}", tags=tag, summary="Delete a vault item (retires canaries)")
    async def delete_item_route(request: Request, ns: str, vault: str, item_id: str,
                                ctx: Ctx = Depends(core.ns_ctx)):
        _require(ctx)
        d = _decl(ctx, vault)
        return await core.run(delete_item, ctx, d, item_id, core.if_match(request))
