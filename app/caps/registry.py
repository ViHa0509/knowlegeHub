"""registry (spec §6.2): namespaces (attach/detach/suspend), manifest (schemas, types, keyspaces, quotas,
subscriptions), ns state cache. Hooks other modules register live in core and are re-exported here."""
from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI, Query, Request

from app import config, sqldb
from app.caps import core
from app.caps.core import CapError

NAME = "registry"
VERSION = "1.0.0"
STATUS = "beta"
DEFAULT_ON = True
SCHEMA_MAX = 65536
TYPES_MAX = 200
INDEX_FIELDS_MAX = 8
LIMITS = {"schema_bytes": SCHEMA_MAX, "types_per_ns": TYPES_MAX, "index_fields_per_type": INDEX_FIELDS_MAX,
          "ns_cache_s": 5, "detach_grace_days": 30, "events_retention_days": 180}
TABLES = ["kh_ns", "kh_schema", "kh_type", "kh_keyspace", "kh_quota", "kh_subscription"]
LOCAL_TABLES: list[str] = []
CRONS: list = []

# hooks (shared lists, see core)
TYPE_USAGE = core.TYPE_USAGE
ON_STATE = core.ON_STATE
ON_ATTACH = core.ON_ATTACH
ON_MANIFEST = core.ON_MANIFEST

STATES = ("active", "suspended", "detached", "purged")
# target state -> allowed source states
TRANSITIONS = {"detached": ("active", "suspended"), "active": ("detached", "suspended"),
               "suspended": ("active",)}
VERB_TARGET = {"detach": "detached", "reattach": "active", "resume": "active", "suspend": "suspended"}

TYPE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
SCHEMA_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
KEYSPACE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
QUOTA_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
SUB_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
KINDS_OF = ("record", "event", "item", "metric")
ENVELOPE = ("run_id", "unit_id", "session_id", "attempt", "epoch")
MANIFEST_KEYS = {"schemas", "types", "keyspaces", "quotas", "retention", "redaction", "vaults", "subscriptions",
                 "settings", "manifest_version", "consumer", "description"}


def grace_days() -> int:
    return config._int("KH_NS_GRACE_DAYS", 30)


def events_retention_days() -> int:
    return config._int("KH_EVENTS_RETENTION_DAYS", 180)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _m1(c) -> None:
    sqldb.create_table(
        c, "kh_ns",
        "CREATE TABLE IF NOT EXISTS kh_ns (ns TEXT PRIMARY KEY, display TEXT, state TEXT NOT NULL, "
        "owner_email TEXT, projects_json TEXT, config_json TEXT, manifest_json TEXT, manifest_sha TEXT, "
        "manifest_rev INTEGER NOT NULL DEFAULT 0, created_ms INTEGER, detached_ms INTEGER, purge_after_ms INTEGER, "
        "rev INTEGER NOT NULL DEFAULT 1)",
        "CREATE COLUMN TABLE kh_ns (ns NVARCHAR(64) PRIMARY KEY, display NVARCHAR(256), state NVARCHAR(16) NOT NULL, "
        "owner_email NVARCHAR(256), projects_json NCLOB, config_json NCLOB, manifest_json NCLOB, "
        "manifest_sha NVARCHAR(64), manifest_rev BIGINT NOT NULL, created_ms BIGINT, detached_ms BIGINT, "
        "purge_after_ms BIGINT, rev BIGINT NOT NULL)")
    sqldb.create_table(
        c, "kh_schema",
        "CREATE TABLE IF NOT EXISTS kh_schema (ns TEXT NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL, "
        "json_schema TEXT, sha256 TEXT, created_ms INTEGER, PRIMARY KEY (ns, name, version))",
        "CREATE COLUMN TABLE kh_schema (ns NVARCHAR(64) NOT NULL, name NVARCHAR(128) NOT NULL, "
        "version BIGINT NOT NULL, json_schema NCLOB, sha256 NVARCHAR(64), created_ms BIGINT, "
        "PRIMARY KEY (ns, name, version))")
    sqldb.create_table(
        c, "kh_type",
        "CREATE TABLE IF NOT EXISTS kh_type (ns TEXT NOT NULL, type TEXT NOT NULL, kind_of TEXT NOT NULL, "
        "schema_name TEXT, current_ver INTEGER, index_fields_json TEXT, read_roles_json TEXT, write_roles_json TEXT, "
        "fenced INTEGER NOT NULL DEFAULT 0, require_if_match INTEGER NOT NULL DEFAULT 0, retention_days INTEGER, "
        "confirm_policy_json TEXT, required_json TEXT, state TEXT NOT NULL DEFAULT 'active', "
        "rev INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (ns, type))",
        "CREATE COLUMN TABLE kh_type (ns NVARCHAR(64) NOT NULL, type NVARCHAR(128) NOT NULL, "
        "kind_of NVARCHAR(16) NOT NULL, schema_name NVARCHAR(128), current_ver BIGINT, index_fields_json NCLOB, "
        "read_roles_json NCLOB, write_roles_json NCLOB, fenced INTEGER NOT NULL, require_if_match INTEGER NOT NULL, "
        "retention_days BIGINT, confirm_policy_json NCLOB, required_json NCLOB, state NVARCHAR(16) NOT NULL, "
        "rev BIGINT NOT NULL, PRIMARY KEY (ns, type))")
    sqldb.create_table(
        c, "kh_keyspace",
        "CREATE TABLE IF NOT EXISTS kh_keyspace (ns TEXT NOT NULL, keyspace TEXT NOT NULL, dims_json TEXT, "
        "config_json TEXT, rev INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (ns, keyspace))",
        "CREATE COLUMN TABLE kh_keyspace (ns NVARCHAR(64) NOT NULL, keyspace NVARCHAR(64) NOT NULL, dims_json NCLOB, "
        "config_json NCLOB, rev BIGINT NOT NULL, PRIMARY KEY (ns, keyspace))")
    sqldb.create_table(
        c, "kh_quota",
        "CREATE TABLE IF NOT EXISTS kh_quota (ns TEXT NOT NULL, name TEXT NOT NULL, limit_value REAL NOT NULL, "
        "window_s INTEGER NOT NULL DEFAULT 60, PRIMARY KEY (ns, name))",
        "CREATE COLUMN TABLE kh_quota (ns NVARCHAR(64) NOT NULL, name NVARCHAR(64) NOT NULL, "
        "limit_value DOUBLE NOT NULL, window_s BIGINT NOT NULL, PRIMARY KEY (ns, name))")
    sqldb.create_table(
        c, "kh_subscription",
        "CREATE TABLE IF NOT EXISTS kh_subscription (ns TEXT NOT NULL, sub_id TEXT NOT NULL, kinds_json TEXT, "
        "url TEXT, secret_ref TEXT, state TEXT NOT NULL DEFAULT 'active', rev INTEGER NOT NULL DEFAULT 1, "
        "PRIMARY KEY (ns, sub_id))",
        "CREATE COLUMN TABLE kh_subscription (ns NVARCHAR(64) NOT NULL, sub_id NVARCHAR(64) NOT NULL, "
        "kinds_json NCLOB, url NVARCHAR(2048), secret_ref NVARCHAR(256), state NVARCHAR(16) NOT NULL, "
        "rev BIGINT NOT NULL, PRIMARY KEY (ns, sub_id))")


def init(c) -> None:
    sqldb.migrate(c, NAME, [(1, _m1)])


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def ns_view(row: dict) -> dict:
    return {"ns": row["ns"], "display": row.get("display"), "state": row.get("state"),
            "owner_email": row.get("owner_email"), "projects": list(row.get("projects") or []),
            "config": row.get("config") or {}, "manifest_rev": int(row.get("manifest_rev") or 0),
            "manifest_sha": row.get("manifest_sha"), "created_ms": row.get("created_ms"),
            "detached_ms": row.get("detached_ms"), "purge_after_ms": row.get("purge_after_ms"),
            "rev": int(row.get("rev") or 0)}


def _get_row(c, ns: str) -> dict | None:
    r = c.execute("SELECT * FROM kh_ns WHERE ns=?", (ns,)).fetchone()
    return core.decode_ns_row(r) if r else None


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


class _Errs:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, pointer: str, message: str) -> None:
        if len(self.items) < 200:
            self.items.append({"pointer": pointer, "message": message})

    def raise_if(self) -> None:
        if self.items:
            raise CapError("invalid_manifest", "the manifest is not valid", extra={"errors": self.items})


def _roles(v: Any, ptr: str, errs: _Errs) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        errs.add(ptr, "must be a list of role names")
        return []
    bad = [x for x in v if x not in core.ROLES and x != core.ADMIN]
    if bad:
        errs.add(ptr, f"unknown roles: {', '.join(bad)}")
    return list(dict.fromkeys(v))


def _list_or_dict(v: Any, keyname: str, ptr: str, errs: _Errs) -> list:
    if v is None:
        return []
    if isinstance(v, dict):
        out = []
        for k, item in v.items():
            if isinstance(item, dict):
                item = dict(item)
                item.setdefault(keyname, k)
                out.append(item)
            else:
                out.append({keyname: k, "_value": item})
        return out
    if isinstance(v, list):
        return v
    errs.add(ptr, "must be a list or an object")
    return []


def parse_manifest(m: Any) -> dict:
    """Validate + normalize. Raises 422 invalid_manifest {errors:[{pointer, message}]}."""
    errs = _Errs()
    if not isinstance(m, dict):
        errs.add("/", "the manifest must be a JSON object")
        errs.raise_if()
    for k in m:
        if k not in MANIFEST_KEYS:
            errs.add(f"/{k}", "unknown key")
    out: dict = {"schemas": {}, "types": {}, "keyspaces": {}, "quotas": {}, "subscriptions": {}, "config": {}}

    # schemas
    for i, s in enumerate(_list_or_dict(m.get("schemas"), "name", "/schemas", errs)):
        p = f"/schemas/{i}"
        if not isinstance(s, dict):
            errs.add(p, "must be an object {name, version, schema}")
            continue
        name, ver, schema = s.get("name"), s.get("version", 1), s.get("schema")
        if "_value" in s and schema is None:
            schema = s["_value"]
        if not isinstance(name, str) or not SCHEMA_NAME_RE.match(name):
            errs.add(f"{p}/name", "schema name must match " + SCHEMA_NAME_RE.pattern)
            continue
        if not isinstance(ver, int) or isinstance(ver, bool) or ver < 1:
            errs.add(f"{p}/version", "version must be an integer >= 1")
            continue
        if schema is None:
            errs.add(f"{p}/schema", "schema is required")
            continue
        if core.json_size(schema) > SCHEMA_MAX:
            errs.add(f"{p}/schema", f"schema is larger than {SCHEMA_MAX} bytes")
            continue
        for msg in core.schema_check(schema)[:core.MAX_SCHEMA_ERRORS]:
            errs.add(f"{p}/schema", f"does not compile as JSON Schema 2020-12: {msg}")
        if (name, ver) in out["schemas"]:
            errs.add(p, f"schema {name} v{ver} is listed twice")
        out["schemas"][(name, ver)] = {"name": name, "version": ver, "schema": schema,
                                       "sha256": core.sha256_json(schema)}

    latest: dict[str, int] = {}
    for (name, ver) in out["schemas"]:
        latest[name] = max(ver, latest.get(name, 0))

    # types
    types = _list_or_dict(m.get("types"), "type", "/types", errs)
    if len(types) > TYPES_MAX:
        errs.add("/types", f"at most {TYPES_MAX} types per namespace")
    for i, t in enumerate(types):
        p = f"/types/{i}"
        if not isinstance(t, dict):
            errs.add(p, "must be an object")
            continue
        name = t.get("type")
        if not isinstance(name, str) or not TYPE_RE.match(name):
            errs.add(f"{p}/type", "type must match " + TYPE_RE.pattern)
            continue
        if name == "kh" or name.startswith("kh."):
            errs.add(f"{p}/type", "types 'kh' and 'kh.*' are reserved")
            continue
        unknown = set(t) - {"type", "kind_of", "schema", "schema_version", "index_fields", "read_roles",
                            "write_roles", "fenced", "require_if_match", "retention_days", "confirm_policy",
                            "required_envelope", "description", "_value"}
        for k in sorted(unknown):
            errs.add(f"{p}/{k}", "unknown key")
        kind_of = t.get("kind_of", "record")
        if kind_of not in KINDS_OF:
            errs.add(f"{p}/kind_of", f"kind_of must be one of {', '.join(KINDS_OF)}")
            continue
        schema_name = t.get("schema")
        schema_ver = t.get("schema_version")
        if schema_name is not None:
            if not isinstance(schema_name, str):
                errs.add(f"{p}/schema", "schema must be a schema name")
                schema_name = None
            elif schema_ver is None:
                schema_ver = latest.get(schema_name)
                if schema_ver is None:
                    errs.add(f"{p}/schema", f"schema '{schema_name}' is not declared in this manifest")
            elif not isinstance(schema_ver, int) or (schema_name, schema_ver) not in out["schemas"]:
                errs.add(f"{p}/schema_version", f"schema {schema_name} v{schema_ver} is not declared")
        ixf = t.get("index_fields") or {}
        if isinstance(ixf, list):
            ixf = {str(x): "s" for x in ixf}
        if not isinstance(ixf, dict):
            errs.add(f"{p}/index_fields", "must be {name: s|n|t} or a list of names")
            ixf = {}
        if len(ixf) > INDEX_FIELDS_MAX:
            errs.add(f"{p}/index_fields", f"at most {INDEX_FIELDS_MAX} index fields")
        allowed_kinds = ("s", "n") if kind_of == "event" else ("s", "n", "t")
        for f, k in ixf.items():
            if k not in allowed_kinds:
                errs.add(f"{p}/index_fields/{f}", f"index kind must be one of {', '.join(allowed_kinds)}")
        req = t.get("required_envelope") or []
        if not isinstance(req, list) or any(x not in ENVELOPE for x in req):
            errs.add(f"{p}/required_envelope", f"must be a subset of {', '.join(ENVELOPE)}")
            req = []
        ret = t.get("retention_days", events_retention_days() if kind_of == "event" else None)
        if ret is not None and (not isinstance(ret, int) or isinstance(ret, bool) or ret < 1):
            errs.add(f"{p}/retention_days", "retention_days must be an integer >= 1 or null")
            ret = None
        cp = t.get("confirm_policy") or {}
        if not isinstance(cp, dict):
            errs.add(f"{p}/confirm_policy", "must be an object")
            cp = {}
        if name in out["types"]:
            errs.add(p, f"type '{name}' is listed twice")
        out["types"][name] = {
            "type": name, "kind_of": kind_of, "schema_name": schema_name, "current_ver": schema_ver,
            "index_spec": {str(f): k for f, k in ixf.items()},
            "read_roles": _roles(t.get("read_roles"), f"{p}/read_roles", errs),
            "write_roles": _roles(t.get("write_roles"), f"{p}/write_roles", errs),
            "fenced": bool(t.get("fenced")), "require_if_match": bool(t.get("require_if_match")),
            "retention_days": ret, "confirm_policy": cp, "required": list(dict.fromkeys(req)),
        }

    # keyspaces
    for i, k in enumerate(_list_or_dict(m.get("keyspaces"), "keyspace", "/keyspaces", errs)):
        p = f"/keyspaces/{i}"
        if not isinstance(k, dict):
            errs.add(p, "must be an object {keyspace, dims}")
            continue
        name, dims = k.get("keyspace"), k.get("dims", k.get("_value"))
        if not isinstance(name, str) or not KEYSPACE_RE.match(name):
            errs.add(f"{p}/keyspace", "keyspace must match " + KEYSPACE_RE.pattern)
            continue
        if not isinstance(dims, list) or not (1 <= len(dims) <= 8) or not all(isinstance(d, str) and d
                                                                               for d in dims):
            errs.add(f"{p}/dims", "dims must be a list of 1..8 names")
            continue
        conf = {x: v for x, v in k.items() if x not in ("keyspace", "dims", "_value")}
        out["keyspaces"][name] = {"keyspace": name, "dims": dims, "config": conf}

    # quotas
    for i, q in enumerate(_list_or_dict(m.get("quotas"), "name", "/quotas", errs)):
        p = f"/quotas/{i}"
        if not isinstance(q, dict):
            errs.add(p, "must be an object {name, limit, window_s}")
            continue
        name, lim, win = q.get("name"), q.get("limit", q.get("_value")), q.get("window_s", 60)
        if not isinstance(name, str) or not QUOTA_RE.match(name):
            errs.add(f"{p}/name", "quota name must match " + QUOTA_RE.pattern)
            continue
        if not isinstance(lim, (int, float)) or isinstance(lim, bool) or lim < 0:
            errs.add(f"{p}/limit", "limit must be a number >= 0")
            continue
        if not isinstance(win, int) or isinstance(win, bool) or win < 1:
            errs.add(f"{p}/window_s", "window_s must be an integer >= 1")
            continue
        out["quotas"][name] = {"name": name, "limit": float(lim), "window_s": int(win)}

    # subscriptions
    import os as _os

    allow_local = (_os.environ.get("KH_OUTBOX_ALLOW_LOCAL") or "").strip() == "1"
    for i, s in enumerate(_list_or_dict(m.get("subscriptions"), "sub_id", "/subscriptions", errs)):
        p = f"/subscriptions/{i}"
        if not isinstance(s, dict):
            errs.add(p, "must be an object {sub_id, kinds, url}")
            continue
        sid, kinds, url = s.get("sub_id"), s.get("kinds"), s.get("url")
        if not isinstance(sid, str) or not SUB_RE.match(sid):
            errs.add(f"{p}/sub_id", "sub_id must match " + SUB_RE.pattern)
            continue
        if not isinstance(kinds, list) or not kinds or not all(isinstance(x, str) and x for x in kinds):
            errs.add(f"{p}/kinds", "kinds must be a non-empty list of event kinds (patterns like run.* allowed)")
            continue
        if url is not None:
            ok = isinstance(url, str) and (url.startswith("https://") or (
                allow_local and re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", url)))
            if not ok:
                errs.add(f"{p}/url", "url must be https://")
                continue
        state = s.get("state", "active")
        if state not in ("active", "paused"):
            errs.add(f"{p}/state", "state must be active or paused")
            continue
        out["subscriptions"][sid] = {"sub_id": sid, "kinds": kinds, "url": url,
                                     "secret_ref": s.get("secret_ref"), "state": state}

    # objects copied into config_json
    for key in ("retention", "redaction", "settings"):
        v = m.get(key)
        if v is None:
            continue
        if not isinstance(v, dict):
            errs.add(f"/{key}", "must be an object")
            continue
        out["config"][key] = v
    gd = (m.get("retention") or {}).get("grace_days") if isinstance(m.get("retention"), dict) else None
    if gd is not None and (not isinstance(gd, int) or isinstance(gd, bool) or gd < 0):
        errs.add("/retention/grace_days", "grace_days must be an integer >= 0")
    vaults = m.get("vaults")
    if vaults is not None:
        if not isinstance(vaults, list):
            errs.add("/vaults", "must be a list")
        else:
            for i, v in enumerate(vaults):
                if not isinstance(v, dict) or not isinstance(v.get("vault_id"), str) or not v.get("vault_id"):
                    errs.add(f"/vaults/{i}/vault_id", "vault_id is required")
            out["config"]["vaults"] = vaults
    for key in ("manifest_version", "consumer", "description"):
        if key in m:
            out[key] = m[key]
    errs.raise_if()
    return out


def assign_columns(existing: dict, wanted: dict, kind_of: str, ptr: str, errs: _Errs) -> dict:
    """{field: kind} -> {field: column}. Existing names keep their column; a field's kind cannot change."""
    out: dict[str, str] = {}
    used = set()
    for f, k in wanted.items():
        col = existing.get(f)
        if col:
            if core.ix_kind(col) != k:
                errs.add(f"{ptr}/index_fields/{f}", f"index field '{f}' cannot change type")
                continue
            out[f] = col
            used.add(col)
    for f, k in wanted.items():
        if f in out:
            continue
        free = [c for c in core.IX_SLOTS[k] if c not in used]
        if not free:
            errs.add(f"{ptr}/index_fields/{f}", f"no free '{k}' index column left")
            continue
        out[f] = free[0]
        used.add(free[0])
    return out


# ---------------------------------------------------------------------------
# apply_manifest
# ---------------------------------------------------------------------------


def apply_manifest(ns: str, manifest: dict, expected_rev: int | None, archive: bool = False) -> dict:
    parsed = parse_manifest(manifest)
    sha = core.sha256_json(manifest)
    with sqldb.connect() as c:
        row = _get_row(c, ns)
        if row is None or row["state"] not in ("active", "suspended"):
            raise CapError("ns_not_active", f"namespace '{ns}' is not active")
        cur_rev = int(row.get("manifest_rev") or 0)
        if row.get("manifest_sha") == sha:
            return {"ns": ns, "changed": False, "manifest_rev": cur_rev, "sha256": sha,
                    "types": sorted(parsed["types"]), "schemas": _schema_list(parsed), "removed_types": []}
        if row.get("manifest") and expected_rev is None:
            raise CapError("precondition_required", "a manifest exists: send If-Match with its manifest_rev",
                           headers={"ETag": core.etag(cur_rev)})
        if expected_rev is not None and expected_rev >= 0 and expected_rev != cur_rev:
            raise core.write_conflict(ns, "manifest", cur_rev, expected_rev)
        old_types = {t["type"]: core.decode_type_row(t)
                     for t in c.execute("SELECT * FROM kh_type WHERE ns=?", (ns,)).fetchall()}
        errs = _Errs()
        for name, t in parsed["types"].items():
            old = old_types.get(name)
            if old and old["kind_of"] != t["kind_of"]:
                errs.add(f"/types/{name}/kind_of", f"kind_of of existing type '{name}' cannot change")
            t["index_fields"] = assign_columns((old or {}).get("index_fields") or {}, t["index_spec"],
                                               t["kind_of"], f"/types/{name}", errs)
        errs.raise_if()
        # schemas (immutable per version)
        now = sqldb.now_ms(c)
        for (name, ver), s in parsed["schemas"].items():
            r = c.execute("SELECT sha256 FROM kh_schema WHERE ns=? AND name=? AND version=?",
                          (ns, name, ver)).fetchone()
            if r is not None:
                if r[0] != s["sha256"]:
                    raise CapError("schema_immutable", f"schema {name} v{ver} already exists with other content; "
                                   "publish a new version", extra={"schema": name, "version": ver})
                continue
            sqldb.insert_new(c, "kh_schema", {"ns": ns, "name": name, "version": ver,
                                              "json_schema": core.dumps(s["schema"]), "sha256": s["sha256"],
                                              "created_ms": now})
        # removed types
        removed, archived = [], []
        for name, old in old_types.items():
            if name in parsed["types"]:
                continue
            used = 0
            for fn in TYPE_USAGE:
                try:
                    used += int(fn(c, ns, name) or 0)
                except Exception:  # noqa: BLE001
                    continue
            if used and old.get("state") == "archived":
                continue  # stays archived while rows use it
            if used:
                if not archive:
                    raise CapError("type_in_use", f"type '{name}' still has {used} rows; resend with ?archive=true",
                                   extra={"type": name, "rows": used})
                c.execute("UPDATE kh_type SET state='archived', rev=rev+1 WHERE ns=? AND type=?", (ns, name))
                archived.append(name)
            else:
                c.execute("DELETE FROM kh_type WHERE ns=? AND type=?", (ns, name))
            removed.append(name)
        # upsert types
        for name, t in parsed["types"].items():
            old = old_types.get(name)
            row_t = {"ns": ns, "type": name, "kind_of": t["kind_of"], "schema_name": t["schema_name"],
                     "current_ver": t["current_ver"], "index_fields_json": core.dumps(t["index_fields"]),
                     "read_roles_json": core.dumps(t["read_roles"]), "write_roles_json": core.dumps(t["write_roles"]),
                     "fenced": int(t["fenced"]), "require_if_match": int(t["require_if_match"]),
                     "retention_days": t["retention_days"], "confirm_policy_json": core.dumps(t["confirm_policy"]),
                     "required_json": core.dumps(t["required"]), "state": "active",
                     "rev": int((old or {}).get("rev") or 0) + 1}
            sqldb.upsert(c, "kh_type", ["ns", "type"], row_t)
        # keyspaces / quotas / subscriptions: replaced wholesale
        c.execute("DELETE FROM kh_keyspace WHERE ns=?", (ns,))
        for k in parsed["keyspaces"].values():
            sqldb.insert_new(c, "kh_keyspace", {"ns": ns, "keyspace": k["keyspace"], "dims_json": core.dumps(k["dims"]),
                                                "config_json": core.dumps(k["config"]), "rev": 1})
        c.execute("DELETE FROM kh_quota WHERE ns=?", (ns,))
        for q in parsed["quotas"].values():
            sqldb.insert_new(c, "kh_quota", {"ns": ns, "name": q["name"], "limit_value": q["limit"],
                                             "window_s": q["window_s"]})
        c.execute("DELETE FROM kh_subscription WHERE ns=?", (ns,))
        for s in parsed["subscriptions"].values():
            sqldb.insert_new(c, "kh_subscription", {"ns": ns, "sub_id": s["sub_id"], "kinds_json": core.dumps(s["kinds"]),
                                                    "url": s["url"], "secret_ref": s["secret_ref"],
                                                    "state": s["state"], "rev": 1})
        conf = dict(row.get("config") or {})
        for key in ("retention", "redaction", "settings", "vaults"):
            conf.pop(key, None)
        conf.update(parsed["config"])
        cur = c.execute("UPDATE kh_ns SET manifest_json=?, manifest_sha=?, manifest_rev=manifest_rev+1, "
                        "config_json=?, rev=rev+1 WHERE ns=? AND manifest_rev=?",
                        (core.dumps(manifest), sha, core.dumps(conf), ns, cur_rev))
        if not cur.rowcount or cur.rowcount <= 0:
            now_rev = c.execute("SELECT manifest_rev FROM kh_ns WHERE ns=?", (ns,)).fetchone()
            raise core.write_conflict(ns, "manifest", int(now_rev[0]) if now_rev else None, expected_rev)
        for fn in ON_MANIFEST:
            fn(c, ns, parsed, row.get("manifest"))
        new_rev = cur_rev + 1
    core.invalidate_ns(ns)
    core.emit(ns, "kh.manifest", {"manifest_rev": new_rev, "sha256": sha, "removed_types": removed,
                                  "archived_types": archived})
    return {"ns": ns, "changed": True, "manifest_rev": new_rev, "sha256": sha, "types": sorted(parsed["types"]),
            "schemas": _schema_list(parsed), "removed_types": removed}


def _schema_list(parsed: dict) -> list[dict]:
    return [{"name": n, "version": v} for (n, v) in sorted(parsed["schemas"])]


# ---------------------------------------------------------------------------
# Namespace admin
# ---------------------------------------------------------------------------


def _validate_projects(projects: Any) -> list[str]:
    if projects is None:
        return []
    if not isinstance(projects, list) or not all(isinstance(p, str) and p.strip() for p in projects):
        raise CapError("invalid", "projects must be a list of project ids")
    from app import projects as kh_projects

    out = list(dict.fromkeys(p.strip() for p in projects))
    unknown = [p for p in out if not kh_projects.exists(p)]
    if unknown:
        raise CapError("invalid", f"unknown projects: {', '.join(unknown)}", extra={"unknown": unknown})
    return out


def put_ns(ns: str, body: dict, expected_rev: int | None) -> tuple[dict, bool]:
    """Attach (new -> created True) or update. Returns (row, created)."""
    projects = _validate_projects(body.get("projects"))
    display = body.get("display")
    if display is not None and not isinstance(display, str):
        raise CapError("invalid", "display must be a string")
    config_in = body.get("config")
    if config_in is not None and not isinstance(config_in, dict):
        raise CapError("invalid", "config must be an object")
    owner = body.get("owner_email")
    if owner is not None and (not isinstance(owner, str) or "@" not in owner):
        raise CapError("invalid", "owner_email must be an e-mail address")
    with sqldb.connect() as c:
        row = _get_row(c, ns)
    if row is not None and row["state"] == "purged" and row.get("purge_after_ms") is not None:
        raise CapError("invalid_transition", f"namespace '{ns}' is being purged; try again later")
    fresh = row is None or row["state"] == "purged"
    if fresh:
        core.check_new_ns(ns)
        caller = core.admin_person(projects, None)
        if owner and caller.kind == "person" and not core.is_maintainer() and owner.lower() != caller.email:
            raise CapError("forbidden", "only a KH maintainer may name another owner")
        owner_email = (owner or caller.email or "").lower() or None
        with sqldb.connect() as c:
            now = sqldb.now_ms(c)
            if row is not None:
                c.execute("DELETE FROM kh_ns WHERE ns=? AND state='purged' AND purge_after_ms IS NULL", (ns,))
            new = {"ns": ns, "display": display or ns, "state": "active", "owner_email": owner_email,
                   "projects_json": core.dumps(projects), "config_json": core.dumps(config_in or {}),
                   "manifest_json": None, "manifest_sha": None, "manifest_rev": 0, "created_ms": now,
                   "detached_ms": None, "purge_after_ms": None, "rev": 1}
            if not sqldb.insert_new(c, "kh_ns", new):
                raise CapError("duplicate", f"namespace '{ns}' already exists")
            out = _get_row(c, ns)
        core.invalidate_ns(ns)
        core.emit(ns, "kh.ns_state", {"ns": ns, "from": None, "to": "active"})
        core.emit(core.KH_NS, "kh.ns_state", {"ns": ns, "from": None, "to": "active"})
        return out, True
    core.admin_person(projects, row)
    if owner and owner.lower() != (row.get("owner_email") or "") and not core.is_maintainer():
        raise CapError("forbidden", "only a KH maintainer may change the owner")
    with sqldb.connect() as c:
        sets = {"projects_json": core.dumps(projects if body.get("projects") is not None else row["projects"])}
        if display is not None:
            sets["display"] = display
        if owner:
            sets["owner_email"] = owner.lower()
        if config_in:
            merged = dict(row.get("config") or {})
            merged.update(config_in)
            sets["config_json"] = core.dumps(merged)
        core.cas_update(c, "kh_ns", {"ns": ns}, sets, expected_rev, ns=ns, what="namespace")
        out = _get_row(c, ns)
    core.invalidate_ns(ns)
    return out, False


def transition(ns: str, target: str, verb: str) -> dict:
    with sqldb.connect() as c:
        row = _get_row(c, ns)
    if row is None or row["state"] == "purged":
        raise CapError("ns_not_active", f"namespace '{ns}' does not exist")
    core.admin_person(row.get("projects") or [], row)
    src = row["state"]
    if src == target:
        return {**ns_view(row), "from": src, "changed": False}
    if src not in TRANSITIONS.get(target, ()):
        raise CapError("invalid_transition", f"cannot {verb} a namespace that is {src}",
                       extra={"from": src, "to": target})
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        if src == "detached" and target == "active" and row.get("purge_after_ms") is not None \
                and int(row["purge_after_ms"]) < now:
            raise CapError("invalid_transition", "the detach grace period is over; the namespace awaits purge",
                           extra={"from": src, "to": target})
        sets = "state=?, rev=rev+1"
        params: list = [target]
        if target == "detached":
            g = (row.get("config") or {}).get("retention", {}).get("grace_days")
            days = int(g) if isinstance(g, int) and not isinstance(g, bool) else grace_days()
            sets += ", detached_ms=?, purge_after_ms=?"
            params += [now, now + days * 86_400_000]
        elif src == "detached":
            sets += ", detached_ms=NULL, purge_after_ms=NULL"
        cur = c.execute(f"UPDATE kh_ns SET {sets} WHERE ns=? AND state=?", params + [ns, src])
        if not cur.rowcount or cur.rowcount <= 0:
            raise CapError("write_conflict", "the namespace changed state concurrently; retry")
        core.bump_leases(c, ns)
        for fn in ON_STATE:
            fn(c, ns, src, target)
        out = _get_row(c, ns)
    core.invalidate_ns(ns)
    ev = {"ns": ns, "from": src, "to": target}
    core.emit(ns, "kh.ns_state", ev)
    core.emit(core.KH_NS, "kh.ns_state", ev)
    return {**ns_view(out), "from": src, "changed": True}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def attach(app: FastAPI) -> None:
    tag = ["v1 registry"]

    def _verb_route(verb: str):
        target = VERB_TARGET[verb]

        async def handler(ns: str):
            out = await core.run(transition, ns, target, verb)
            return core.reply(out, etag_rev=out["rev"])

        handler.__name__ = f"ns_{verb}"
        return handler

    for verb in ("detach", "reattach", "resume", "suspend"):
        app.add_api_route(f"/v1/admin/ns/{{ns:ns}}:{verb}", _verb_route(verb), methods=["POST"], tags=tag,
                          summary=f"{verb.capitalize()} a namespace")

    @app.get("/v1/admin/ns", tags=tag, summary="List namespaces")
    async def list_ns():
        def _do():
            from app import auth

            p = auth.current()
            if p is not None and p.ns:
                raise CapError("forbidden", "service tokens cannot manage namespaces")
            with sqldb.connect() as c:
                rows = [core.decode_ns_row(r) for r in c.execute("SELECT * FROM kh_ns ORDER BY ns").fetchall()]
            if not core.is_maintainer():
                me = (p.email if p else "").lower()
                rows = [r for r in rows if (r.get("owner_email") or "").lower() == me]
            return {"namespaces": [ns_view(r) for r in rows if r["state"] != "purged" or core.is_maintainer()]}

        return await core.run(_do)

    @app.put("/v1/admin/ns/{ns:ns}", tags=tag, summary="Attach or update a namespace")
    async def put_namespace(ns: str, request: Request):
        body = await core.read_object(request)
        exp = core.if_match(request)
        row, created = await core.run(put_ns, ns, body, exp)
        out = ns_view(row)
        if created:
            client: dict = {}
            for fn in ON_ATTACH:
                try:
                    r = await core.run(fn, ns, row, request)
                except CapError as e:
                    r = e.body()
                if r:
                    client.update(r)
            out["client"] = client or None
        return core.reply(out, status=201 if created else 200, etag_rev=out["rev"])

    @app.get("/v1/admin/ns/{ns:ns}", tags=tag, summary="Read a namespace")
    async def get_namespace(ns: str):
        def _do():
            with sqldb.connect() as c:
                row = _get_row(c, ns)
            if row is None:
                raise CapError("not_found", f"namespace '{ns}' does not exist")
            core.admin_person([], row)
            return row

        row = await core.run(_do)
        return core.reply(ns_view(row), etag_rev=int(row["rev"]))

    @app.delete("/v1/admin/ns/{ns:ns}", tags=tag, summary="Detach a namespace")
    async def delete_namespace(ns: str):
        out = await core.run(transition, ns, "detached", "detach")
        return core.reply(out, etag_rev=out["rev"])

    @app.put("/v1/{ns}/manifest", tags=tag, summary="Apply the namespace manifest")
    async def put_manifest(ns: str, request: Request, archive: bool = Query(False)):
        ctx = await core.run(core.ns_ctx, request, ns)
        ctx.require_admin()
        body = await core.read_json(request)
        out = await core.run(apply_manifest, ns, body, core.if_match(request), archive)
        return core.reply(out, etag_rev=out["manifest_rev"])

    @app.get("/v1/{ns}/manifest", tags=tag, summary="Read the namespace manifest")
    async def get_manifest(ns: str, request: Request):
        ctx = await core.run(core.ns_ctx, request, ns)
        row = ctx.row
        rev = int(row.get("manifest_rev") or 0)
        return core.reply({"ns": ns, "manifest_rev": rev, "manifest": row.get("manifest")}, etag_rev=rev)
