"""Stage F shared plumbing (spec §6.1): namespace ids, errors, roles, caller, `ns_ctx`, quotas, fencing,
revisions, idempotency, kh.* emission, index columns, schema validation, small HTTP helpers.

Not a capability module (no NAME, no routes). Every /v1 module builds on it; the public helpers are listed in
docs/integration-notes.md under "§6 caps core".
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from fastapi import Request
from fastapi.responses import JSONResponse, Response

from app import config, sqldb

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Namespace ids
# ---------------------------------------------------------------------------

NS_RE = re.compile(r"^[a-z][a-z0-9_]{2,31}$")
RESERVED = frozenset({"admin", "me", "capabilities", "token", "redaction", "openapi", "health", "version"})
RESERVED_PREFIXES = ("kh", "_")
KH_NS = "_kh"            # KH's own jobs namespace (cron leases, system events)
PEOPLE_NS = "kh_people"  # KH's own internal namespace (people module)


def ns_valid(ns: str) -> bool:
    """Syntactically valid ns id (reserved ids included)."""
    return bool(ns) and bool(NS_RE.match(ns))


def ns_reserved(ns: str) -> bool:
    return ns in RESERVED or any(ns.startswith(p) for p in RESERVED_PREFIXES)


def check_new_ns(ns: str) -> None:
    """Refuse an id a consumer may not attach (422 invalid)."""
    if not ns_valid(ns or ""):
        raise CapError("invalid", f"namespace id must match {NS_RE.pattern}")
    if ns_reserved(ns):
        raise CapError("invalid", f"namespace id '{ns}' is reserved")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

CODES: dict[str, int] = {
    "ns_not_active": 404, "ns_suspended": 423, "not_found": 404, "forbidden": 403, "unauthorized": 401,
    "invalid": 422, "validation_failed": 422, "invalid_manifest": 422, "schema_immutable": 409,
    "type_in_use": 409, "precondition_required": 428, "write_conflict": 409, "stale_epoch": 409,
    "lease_held": 409, "lease_lost": 409, "idempotency_mismatch": 422, "in_progress": 409, "quota": 429,
    "duplicate": 409, "effect_claimed": 409, "too_large": 413, "provisioner_unavailable": 503,
    "record_final": 409, "pack_immutable": 409, "redaction_refused": 422, "vault_not_declared": 404,
    "canary_conflict": 409, "version_order": 409, "assumption_final": 409, "leak_refused": 422,
    "lint_failed": 422, "invalid_transition": 409, "approval_required": 428, "improve_off": 409,
    "embedder_unavailable": 503,
}


class CapError(Exception):
    """A /v1 refusal: JSON `{"error": code, "detail": detail, **extra}` with the code's HTTP status.

    kh_events: [(ns, kind, body), ...] written by the handler AFTER the transaction rolled back.
    """

    def __init__(self, code: str, detail: Any = "", status: int | None = None, extra: dict | None = None,
                 headers: dict | None = None, kh_events: Iterable | None = None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = int(status or CODES.get(code, 400))
        self.extra = dict(extra or {})
        self.headers = dict(headers or {})
        self.kh_events = list(kh_events or [])

    def body(self) -> dict:
        return {"error": self.code, "detail": self.detail, **self.extra}


async def cap_error_handler(_request: Request, exc: CapError) -> JSONResponse:
    for ev in exc.kh_events:
        try:
            ns, kind, body = ev[0], ev[1], (ev[2] if len(ev) > 2 else {})
            await asyncio.to_thread(emit, ns, kind, body)
        except Exception as e:  # noqa: BLE001
            log.debug("kh event of error not written: %s", e)
    return JSONResponse(exc.body(), status_code=exc.status, headers=exc.headers or None)


class _NsConvertor:
    """Path convertor `{ns:ns}`: one segment without ':' so `/v1/admin/ns/{ns:ns}` never swallows
    `/v1/admin/ns/x:verb` (register verb routes with `{ns:ns}:verb`)."""

    regex = "[^/:]+"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return str(value)


def _register_convertor() -> None:
    from starlette.convertors import CONVERTOR_TYPES, register_url_convertor

    if "ns" not in CONVERTOR_TYPES:
        register_url_convertor("ns", _NsConvertor())


_register_convertor()


def install(app) -> None:
    """Install the CapError handler once."""
    if getattr(app.state, "_caps_error_handler", False):
        return
    app.add_exception_handler(CapError, cap_error_handler)
    app.state._caps_error_handler = True


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

ROLES = ("orchestrator", "agent", "judge", "grader", "distiller", "confirmer")
ADMIN = "admin"


def _m(**areas: str) -> dict[str, frozenset]:
    return {a: frozenset(v.split(",")) for a, v in areas.items()}


MATRIX: dict[str, dict[str, frozenset]] = {
    "orchestrator": _m(events="append,read", records="read,write", rules="read,write", packs="read,write",
                       items="read", retrieval="read", worklog="append", metrics="write,read",
                       admin="principals,leases", leases="use", effects="use"),
    "agent": _m(events="append", records="read,write", rules="read,write", packs="read", items="read,propose",
                retrieval="read", worklog="append", metrics="write", leases="use", effects="use"),
    "judge": _m(events="append,read", records="read,write", rules="read", packs="read", items="read",
                retrieval="read", worklog="append", metrics="read", leases="use", effects="use"),
    "grader": _m(events="append,read", records="read", rules="read", packs="read", items="read", vault="read",
                 metrics="write,read", leases="use", effects="use"),
    "distiller": _m(events="read", records="read", rules="read", packs="read", items="propose,write,read",
                    retrieval="read", metrics="read"),
    "confirmer": _m(events="read", records="read", rules="read,confirm", packs="read,confirm",
                    items="read,confirm,withdraw", retrieval="read", metrics="read"),
}


def role_allows(role: str | None, area: str, action: str) -> bool:
    if role == ADMIN:
        return True
    return action in MATRIX.get(role or "", {}).get(area, frozenset())


def acl_allows(role: str | None, roles: Iterable[str] | None) -> bool:
    """Type ACL (`read_roles`/`write_roles`): can only narrow; empty = matrix alone."""
    roles = list(roles or [])
    return role == ADMIN or not roles or role in roles


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id() -> str:
    """Time-ordered id: 12 hex ms + 12 hex random."""
    return f"{now_ms():012x}"[-12:] + os.urandom(6).hex()


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_text(s: str | bytes) -> str:
    return hashlib.sha256(s.encode("utf-8") if isinstance(s, str) else s).hexdigest()


def sha256_json(obj: Any) -> str:
    return sha256_text(canonical(obj))


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(raw: Any, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def json_size(obj: Any) -> int:
    return len(dumps(obj).encode("utf-8"))


def check_size(obj: Any, limit: int, what: str = "body") -> None:
    n = json_size(obj)
    if n > limit:
        raise CapError("too_large", f"{what} is {n} bytes; the limit is {limit}", extra={"limit": limit})


def clamp_limit(value: Any, default: int, maximum: int, minimum: int = 1) -> int:
    """Pagination limit: blank -> default; non-int -> 422; clamped to [minimum, maximum]."""
    if value is None or value == "":
        return default
    try:
        v = int(value)
    except (TypeError, ValueError):
        raise CapError("invalid", "limit must be an integer")
    return max(minimum, min(maximum, v))


def parse_int(value: Any, name: str, default: int | None = None, minimum: int | None = None,
              maximum: int | None = None) -> int | None:
    """Optional integer field/param; non-int or out of range -> 422 invalid."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise CapError("invalid", f"{name} must be an integer")
    try:
        v = int(value)
        if isinstance(value, float) and value != v:
            raise ValueError
    except (TypeError, ValueError):
        raise CapError("invalid", f"{name} must be an integer")
    if minimum is not None and v < minimum or maximum is not None and v > maximum:
        lo = "" if minimum is None else str(minimum)
        hi = "" if maximum is None else str(maximum)
        raise CapError("invalid", f"{name} must be in [{lo}..{hi}]")
    return v


def csv(value: Any) -> list[str]:
    """`a,b` or list -> trimmed non-empty strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value).split(",")
    return [str(x).strip() for x in items if str(x).strip()]


def parse_ms(value: Any, name: str = "since") -> int | None:
    """Epoch ms (int), epoch seconds (< 1e11) or ISO-8601 -> epoch ms."""
    if value is None or value == "":
        return None
    try:
        v = float(value)
        return int(v * 1000) if v < 1e11 else int(v)
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime, timezone

        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(d.timestamp() * 1000)
    except ValueError:
        raise CapError("invalid", f"{name} must be epoch ms or ISO-8601")


def reply(body: Any, status: int = 200, headers: dict | None = None, etag_rev: int | None = None) -> JSONResponse:
    """JSON response; `etag_rev` adds `ETag: "<rev>"`."""
    h = dict(headers or {})
    if etag_rev is not None:
        h["ETag"] = etag(etag_rev)
    return JSONResponse(body, status_code=status, headers=h or None)


async def read_json(request: Request, required: bool = True, default: Any = None) -> Any:
    """Request body as JSON (blank body -> default/{}); bad JSON -> 422 invalid."""
    raw = await request.body()
    if not raw or not raw.strip():
        if required and default is None:
            return {}
        return {} if default is None else default
    try:
        return json.loads(raw)
    except ValueError:
        raise CapError("invalid", "request body is not valid JSON")


async def read_object(request: Request) -> dict:
    body = await read_json(request)
    if not isinstance(body, dict):
        raise CapError("invalid", "request body must be a JSON object")
    return body


def run(fn: Callable, *args, **kwargs):
    """Blocking work from an async route: `await core.run(fn, ...)`."""
    return asyncio.to_thread(fn, *args, **kwargs)


def split_verb(path_value: str, verbs: Iterable[str]) -> tuple[str, str | None]:
    """`name:verb` -> (name, verb) when verb is one of `verbs`; else (path_value, None)."""
    head, sep, tail = path_value.rpartition(":")
    if sep and tail in set(verbs):
        return head, tail
    return path_value, None


# ---------------------------------------------------------------------------
# Hooks (registered by modules; registry re-exports them)
# ---------------------------------------------------------------------------

CALLER_RESOLVERS: list[Callable[[Request, dict], "Caller | None"]] = []
BODY_FILTERS: list[Callable[["Ctx", str, Any], Any]] = []      # fn(ctx, where, body) -> body (may raise)
TYPE_USAGE: list[Callable[[Any, str, str], int]] = []          # fn(c, ns, type) -> rows using it
ON_STATE: list[Callable[[Any, str, str, str], None]] = []      # fn(c, ns, from, to) inside the transaction
ON_ATTACH: list[Callable[[str, dict, Request], dict | None]] = []  # fn(ns, row, request) after commit
ON_MANIFEST: list[Callable[[Any, str, dict, dict | None], None]] = []  # fn(c, ns, new, old) in transaction
SYSTEM_EMITTER: list[Callable[..., None]] = []                # events.append_system when events is on


def apply_body_filters(ctx: "Ctx", where: str, body: Any) -> Any:
    """Run BODY_FILTERS (redaction, vault canaries). Must run outside a transaction."""
    for fn in BODY_FILTERS:
        body = fn(ctx, where, body)
    return body


# ---------------------------------------------------------------------------
# kh.* events
# ---------------------------------------------------------------------------


def emit(ns: str, kind: str, body: dict | None = None, ctx: "Ctx | None" = None, project: str = "") -> None:
    """Write a `kh.*` event in its own transaction (never raises). Call it AFTER your transaction committed."""
    try:
        if SYSTEM_EMITTER:
            SYSTEM_EMITTER[0](ns, kind, body or {}, ctx=ctx, project=project)
    except Exception as e:  # noqa: BLE001
        log.debug("emit %s/%s failed: %s", ns, kind, e)


# ---------------------------------------------------------------------------
# Namespace snapshot (5 s cache; registry tables)
# ---------------------------------------------------------------------------


def cache_s() -> float:
    return config._float("KH_NS_CACHE_S", 5.0)


@dataclass
class NsState:
    row: dict
    types: dict = field(default_factory=dict)       # type -> decoded kh_type row
    quotas: dict = field(default_factory=dict)      # name -> (limit, window_s)
    keyspaces: dict = field(default_factory=dict)   # keyspace -> {dims, config}
    schemas: dict = field(default_factory=dict)     # (name, version) -> {schema, sha256}
    loaded: float = 0.0

    @property
    def ns(self) -> str:
        return self.row["ns"]

    @property
    def state(self) -> str:
        return self.row.get("state") or ""

    @property
    def projects(self) -> list[str]:
        return list(self.row.get("projects") or [])

    @property
    def config(self) -> dict:
        return self.row.get("config") or {}

    @property
    def manifest(self) -> dict:
        return self.row.get("manifest") or {}

    def settings(self, section: str | None = None) -> dict:
        s = self.config.get("settings") or {}
        return (s.get(section) or {}) if section else s

    def type(self, name: str) -> dict | None:
        return self.types.get(name)

    def schema_for(self, trow: dict) -> dict | None:
        if not trow or not trow.get("schema_name"):
            return None
        s = self.schemas.get((trow["schema_name"], int(trow.get("current_ver") or 0)))
        return s["schema"] if s else None


_ns_cache: dict[str, NsState | None] = {}
_ns_cache_t: dict[str, float] = {}
_ns_lock = threading.Lock()

NS_JSON_FIELDS = {"projects_json": "projects", "config_json": "config", "manifest_json": "manifest"}
TYPE_JSON_FIELDS = {"index_fields_json": "index_fields", "read_roles_json": "read_roles",
                    "write_roles_json": "write_roles", "confirm_policy_json": "confirm_policy",
                    "required_json": "required"}


def decode_ns_row(r: Any) -> dict:
    d = {k: r[k] for k in r.keys()}
    for src, dst in NS_JSON_FIELDS.items():
        d[dst] = loads(d.pop(src, None), [] if dst == "projects" else ({} if dst == "config" else None))
    return d


def decode_type_row(r: Any) -> dict:
    d = {k: r[k] for k in r.keys()}
    for src, dst in TYPE_JSON_FIELDS.items():
        d[dst] = loads(d.pop(src, None), {} if dst in ("index_fields", "confirm_policy") else [])
    d["fenced"] = bool(d.get("fenced"))
    d["require_if_match"] = bool(d.get("require_if_match"))
    return d


def load_ns(c, ns: str) -> NsState | None:
    """Read the namespace snapshot with an open connection (no cache)."""
    if not sqldb.table_exists(c, "kh_ns"):
        return None
    r = c.execute("SELECT * FROM kh_ns WHERE ns=?", (ns,)).fetchone()
    if r is None:
        return None
    st = NsState(row=decode_ns_row(r), loaded=time.monotonic())
    if sqldb.table_exists(c, "kh_type"):
        for t in c.execute("SELECT * FROM kh_type WHERE ns=?", (ns,)).fetchall():
            d = decode_type_row(t)
            st.types[d["type"]] = d
    if sqldb.table_exists(c, "kh_quota"):
        for q in c.execute("SELECT name, limit_value, window_s FROM kh_quota WHERE ns=?", (ns,)).fetchall():
            st.quotas[q[0]] = (float(q[1]), int(q[2] or 60))
    if sqldb.table_exists(c, "kh_keyspace"):
        for k in c.execute("SELECT keyspace, dims_json, config_json FROM kh_keyspace WHERE ns=?", (ns,)).fetchall():
            st.keyspaces[k[0]] = {"dims": loads(k[1], []), "config": loads(k[2], {})}
    if sqldb.table_exists(c, "kh_schema"):
        for s in c.execute("SELECT name, version, json_schema, sha256 FROM kh_schema WHERE ns=?", (ns,)).fetchall():
            st.schemas[(s[0], int(s[1]))] = {"schema": loads(s[2], {}), "sha256": s[3]}
    return st


def ns_state(ns: str, fresh: bool = False) -> NsState | None:
    """Cached snapshot (KH_NS_CACHE_S, default 5 s). None when the ns was never attached."""
    now = time.monotonic()
    if not fresh:
        t = _ns_cache_t.get(ns)
        if t is not None and now - t < cache_s():
            return _ns_cache.get(ns)
    with sqldb.connect() as c:
        st = load_ns(c, ns)
    with _ns_lock:
        _ns_cache[ns] = st
        _ns_cache_t[ns] = now
    return st


def invalidate_ns(ns: str | None = None) -> None:
    with _ns_lock:
        if ns is None:
            _ns_cache.clear()
            _ns_cache_t.clear()
        else:
            _ns_cache.pop(ns, None)
            _ns_cache_t.pop(ns, None)


# ---------------------------------------------------------------------------
# Caller and context
# ---------------------------------------------------------------------------


@dataclass
class Caller:
    principal: str
    role: str
    kind: str                       # person | client | service | local
    on_behalf_of: str | None = None
    projects: list = field(default_factory=list)   # ["*"] = no narrowing
    email: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == ADMIN

    def as_dict(self) -> dict:
        return {"principal": self.principal, "role": self.role, "kind": self.kind,
                "on_behalf_of": self.on_behalf_of, "projects": list(self.projects)}


def _auth():
    from app import auth

    return auth


def person_projects(p) -> list[str]:
    """Projects a signed-in person can read (["*"] = all)."""
    vis = p.visible_projects("project.read")
    return ["*"] if vis is None else sorted(vis)


def is_ns_admin_person(p, row: dict | None) -> bool:
    """KH maintainer, or the ns owner (signed-in person)."""
    if p is None or getattr(p, "ns", None):
        return False
    if p.is_maintainer:
        return True
    owner = ((row or {}).get("owner_email") or "").lower()
    return bool(owner) and owner == (p.email or "").lower()


def default_caller(request: Request, row: dict) -> Caller:
    auth = _auth()
    p = auth.current()
    if p is None:
        if auth.enabled():
            raise CapError("unauthorized", "sign-in required")
        return Caller("local", ADMIN, "local", None, ["*"], None)
    if p.ns:
        if p.ns != row.get("ns"):
            raise CapError("forbidden", "this token belongs to another namespace")
        raise CapError("unauthorized", "service principals are not enabled on this KH")
    if p.is_maintainer:
        return Caller(p.email, ADMIN, "person", None, ["*"], p.email)
    if is_ns_admin_person(p, row):
        return Caller(p.email, ADMIN, "person", None, person_projects(p), p.email)
    raise CapError("forbidden", f"you have no role in namespace '{row.get('ns')}'")


def resolve_caller(request: Request, row: dict) -> Caller:
    for fn in CALLER_RESOLVERS:
        c = fn(request, row)
        if c is not None:
            return c
    return default_caller(request, row)


def effective_projects(ns_projects: list[str], caller_projects: list[str], ns: str = "") -> list[str]:
    """caller ∩ ns. ["*"] means every project (only for kh_people without projects, or ns-less callers)."""
    nsp = list(ns_projects or [])
    if not nsp and ns == PEOPLE_NS:
        return list(caller_projects or [])
    if "*" in (caller_projects or []):
        return nsp
    allowed = set(caller_projects or [])
    return [p for p in nsp if p in allowed]


ENVELOPE_HEADERS = {"run_id": "x-kh-run", "unit_id": "x-kh-unit", "session_id": "x-kh-session",
                    "attempt": "x-kh-attempt"}


@dataclass
class Ctx:
    ns: str
    st: NsState
    caller: Caller
    request: Request | None = None
    run_id: str | None = None
    unit_id: str | None = None
    session_id: str | None = None
    attempt: int | None = None
    lease: str | None = None
    epoch: int | None = None

    # -- caller ---------------------------------------------------------------------------------------------
    @property
    def role(self) -> str:
        return self.caller.role

    @property
    def principal(self) -> str:
        return self.caller.principal

    @property
    def is_admin(self) -> bool:
        return self.caller.is_admin

    @property
    def row(self) -> dict:
        return self.st.row

    def allows(self, area: str, action: str) -> bool:
        return role_allows(self.role, area, action)

    def require(self, area: str, action: str) -> None:
        """403 forbidden unless the caller's role holds `area:action` (admin holds all)."""
        if not self.allows(area, action):
            raise CapError("forbidden", f"role '{self.role}' may not {action} {area}",
                           extra={"area": area, "action": action})

    def require_admin(self) -> None:
        if not self.is_admin:
            raise CapError("forbidden", "namespace admin only")

    def type_may(self, trow: dict | None, op: str) -> bool:
        """Type ACL check (op read|write); can only narrow the matrix."""
        if trow is None:
            return self.is_admin
        return acl_allows(self.role, trow.get("read_roles" if op == "read" else "write_roles"))

    def actor(self) -> dict:
        return {"principal": self.caller.principal, "role": self.caller.role,
                "on_behalf_of": self.caller.on_behalf_of, "run_id": self.run_id, "unit_id": self.unit_id,
                "session_id": self.session_id}

    def actor_json(self) -> str:
        return dumps(self.actor())

    # -- projects -------------------------------------------------------------------------------------------
    def projects(self) -> list[str]:
        """Caller's effective projects in this ns (["*"]-free unless kh_people without projects)."""
        return effective_projects(self.st.projects, self.caller.projects, self.ns)

    def may_project(self, project: str | None) -> bool:
        """Read visibility of a row's project ("" = ns-wide, always visible)."""
        if not project:
            return True
        eff = self.projects()
        return "*" in eff or project in eff

    def require_project(self, project: str | None) -> str:
        """Blank -> the ns's only project ("" if none; 422 if several). Must be in ns + caller projects."""
        p = (project or "").strip()
        nsp = self.st.projects
        if not p:
            if not nsp:
                return ""
            if len(nsp) == 1:
                p = nsp[0]
            else:
                raise CapError("invalid", "name the project: this namespace has several",
                               extra={"projects": nsp})
        if nsp and p not in nsp:
            raise CapError("invalid", f"project '{p}' is not attached to namespace '{self.ns}'")
        if not self.may_project(p):
            raise CapError("forbidden", f"no grant on project '{p}'")
        return p

    # -- manifest -------------------------------------------------------------------------------------------
    def type(self, name: str) -> dict | None:
        t = self.st.type(name)
        return t if t and t.get("state", "active") == "active" else None

    def settings(self, section: str | None = None) -> dict:
        return self.st.settings(section)


def _envelope(request: Request, ctx: Ctx) -> None:
    h = request.headers
    ctx.run_id = (h.get("x-kh-run") or "").strip() or None
    ctx.unit_id = (h.get("x-kh-unit") or "").strip() or None
    ctx.session_id = (h.get("x-kh-session") or "").strip() or None
    a = (h.get("x-kh-attempt") or "").strip()
    if a:
        try:
            ctx.attempt = int(a)
        except ValueError:
            raise CapError("invalid", "X-KH-Attempt must be an integer")
    ctx.lease, ctx.epoch = fence_headers(request)


def ns_ctx(request: Request, ns: str) -> Ctx:
    """FastAPI dependency for every /v1/{ns}/... route (sync: runs in the threadpool)."""
    st = ns_state(ns) if ns_valid(ns) else None
    if st is None or st.state not in ("active", "suspended"):
        raise CapError("ns_not_active", f"namespace '{ns}' is not active")
    if st.state == "suspended" and request.method not in ("GET", "HEAD", "OPTIONS"):
        raise CapError("ns_suspended", f"namespace '{ns}' is suspended; writes are refused")
    caller = resolve_caller(request, st.row)
    rate_check(ns, caller.principal, st)
    ctx = Ctx(ns=ns, st=st, caller=caller, request=request)
    _envelope(request, ctx)
    return ctx


def make_ctx(ns: str, caller: Caller | None = None, st: NsState | None = None) -> Ctx | None:
    """Internal ctx (crons, KH-owned namespaces): admin caller unless given."""
    st = st or ns_state(ns)
    if st is None:
        return None
    return Ctx(ns=ns, st=st, caller=caller or Caller("kh", ADMIN, "local", None, ["*"], None))


# ---------------------------------------------------------------------------
# Rate quotas (token buckets per instance)
# ---------------------------------------------------------------------------

DEFAULT_QUOTAS = {"requests_per_min_principal": (1200.0, 60), "requests_per_min_ns": (20000.0, 60)}
_buckets: dict[tuple, list] = {}
_bucket_lock = threading.Lock()
_quota_event_t: dict[tuple, float] = {}


def instance_count() -> int:
    return max(1, config._int("KH_INSTANCE_COUNT", 1))


def _take(key: tuple, limit: float, window_s: int) -> float | None:
    """Take one token; None = ok, else seconds until one is available."""
    cap = float(limit) / instance_count()
    if cap <= 0:
        return float(window_s)
    rate = cap / max(1, int(window_s))
    now = time.monotonic()
    with _bucket_lock:
        b = _buckets.get(key)
        if b is None or b[2] != cap:
            b = [cap, now, cap]
            _buckets[key] = b
        b[0] = min(cap, b[0] + (now - b[1]) * rate)
        b[1] = now
        if b[0] >= 1.0:
            b[0] -= 1.0
            return None
        return (1.0 - b[0]) / rate


def rate_check(ns: str, principal: str, st: NsState | None = None) -> None:
    quotas = dict(DEFAULT_QUOTAS)
    if st is not None:
        for name in DEFAULT_QUOTAS:
            if name in st.quotas:
                quotas[name] = st.quotas[name]
    for name, key in (("requests_per_min_principal", (ns, "p", principal)), ("requests_per_min_ns", (ns, "n"))):
        limit, window = quotas[name]
        wait = _take(key, limit, window)
        if wait is None:
            continue
        evs = []
        t = time.monotonic()
        if t - _quota_event_t.get(key, -1e9) >= 10.0:
            _quota_event_t[key] = t
            evs.append((ns, "kh.quota", {"quota": name, "principal": principal, "limit": limit,
                                         "window_s": window}))
        raise CapError("quota", f"quota {name} exceeded", extra={"quota": name},
                       headers={"Retry-After": str(max(1, math.ceil(wait)))}, kh_events=evs)


def reset_buckets() -> None:
    with _bucket_lock:
        _buckets.clear()
        _quota_event_t.clear()


# ---------------------------------------------------------------------------
# Fencing
# ---------------------------------------------------------------------------


def fence_headers(request: Request) -> tuple[str | None, int | None]:
    """X-KH-Lease + X-KH-Epoch: both or neither (else 422)."""
    lease = (request.headers.get("x-kh-lease") or "").strip() or None
    epoch_raw = (request.headers.get("x-kh-epoch") or "").strip() or None
    if (lease is None) != (epoch_raw is None):
        raise CapError("invalid", "send X-KH-Lease and X-KH-Epoch together")
    if epoch_raw is None:
        return None, None
    try:
        return lease, int(epoch_raw)
    except ValueError:
        raise CapError("invalid", "X-KH-Epoch must be an integer")


def fence_required(ctx: Ctx, required: bool) -> None:
    """Fenced type without headers -> 428 precondition_required (call before the transaction)."""
    if required and ctx.lease is None:
        raise CapError("precondition_required", "this type is fenced: send X-KH-Lease and X-KH-Epoch")


def fence_ok(c, ns: str, lease: str, epoch: int) -> bool:
    r = c.execute(f"SELECT 1 FROM DUMMY WHERE {sqldb.fence_sql(c)}" if sqldb.dialect(c) == "hana"
                  else f"SELECT 1 WHERE {sqldb.fence_sql(c)}", (ns, lease, int(epoch))).fetchone()
    return r is not None


def fence_error(c, ctx: Ctx) -> CapError:
    cur = sqldb.lease_get(c, ctx.ns, ctx.lease or "")
    current = cur["epoch"] if cur else None
    return CapError("stale_epoch", f"lease '{ctx.lease}' epoch {ctx.epoch} is not current",
                    extra={"lease": ctx.lease, "epoch": ctx.epoch, "current_epoch": current},
                    kh_events=[(ctx.ns, "kh.stale_epoch", {"lease": ctx.lease, "epoch": ctx.epoch,
                                                           "current_epoch": current,
                                                           "principal": ctx.principal})])


def fence(ctx: Ctx, c, required: bool = False) -> None:
    """Inside the write transaction: the lease named by the headers must be held at that epoch."""
    if ctx.lease is None:
        fence_required(ctx, required)
        return
    if not fence_ok(c, ctx.ns, ctx.lease, int(ctx.epoch or 0)):
        raise fence_error(c, ctx)


def bump_leases(c, ns: str) -> int:
    """§6.1 lease_bump_all: epoch+1, holder NULL, until 0 for every lease of a ns (fences in-flight writes)."""
    cur = c.execute("UPDATE kh_lease SET epoch=epoch+1, holder=NULL, until_ms=0 WHERE ns=?", (ns,))
    return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# Revisions / ETag / If-Match
# ---------------------------------------------------------------------------


def etag(rev: int) -> str:
    return f'"{int(rev)}"'


def parse_if_match(value: str | None) -> int | None:
    """`"7"`, `W/"7"`, `7` -> 7; `*` -> -1 (any); blank -> None; junk -> 422."""
    if value is None:
        return None
    v = value.strip()
    if not v:
        return None
    if v == "*":
        return -1
    if v.startswith("W/"):
        v = v[2:]
    v = v.strip().strip('"')
    try:
        return int(v)
    except ValueError:
        raise CapError("invalid", "If-Match must be a revision like \"7\"")


def if_match(request: Request) -> int | None:
    return parse_if_match(request.headers.get("if-match"))


def require_if_match(request: Request, required: bool) -> int | None:
    v = if_match(request)
    if required and v is None:
        raise CapError("precondition_required", "send If-Match with the current revision (ETag)")
    return v


def write_conflict(ns: str, what: str, current: int | None, expected: int | None) -> CapError:
    h = {"ETag": etag(current)} if current is not None else {}
    return CapError("write_conflict", f"{what} changed (revision {current}, you sent {expected})",
                    extra={"current": current}, headers=h,
                    kh_events=[(ns, "kh.write_conflict", {"what": what, "current": current,
                                                          "expected": expected})])


def cas_update(c, table: str, keys: dict, sets: dict, expected_rev: int | None, ns: str = "",
               what: str | None = None) -> int:
    """`UPDATE table SET sets, rev=rev+1 WHERE keys [AND rev=?]`. Returns the new rev.

    expected_rev None or -1 = any. 0 rows -> 404 not_found (row missing) or 409 write_conflict {current}.
    """
    cols = list(sets)
    sql = (f"UPDATE {table} SET {', '.join(f'{k}=?' for k in cols)}{', ' if cols else ''}rev=rev+1 "
           f"WHERE {' AND '.join(f'{k}=?' for k in keys)}")
    params = [sets[k] for k in cols] + [keys[k] for k in keys]
    if expected_rev is not None and expected_rev >= 0:
        sql += " AND rev=?"
        params.append(int(expected_rev))
    cur = c.execute(sql, params)
    where = " AND ".join(f"{k}=?" for k in keys)
    r = c.execute(f"SELECT rev FROM {table} WHERE {where}", [keys[k] for k in keys]).fetchone()
    if not cur.rowcount or cur.rowcount <= 0:
        if r is None:
            raise CapError("not_found", f"{what or table} not found")
        raise write_conflict(ns, what or table, int(r[0]), expected_rev)
    return int(r[0])


# ---------------------------------------------------------------------------
# Idempotency (table kh_idem owned by coord)
# ---------------------------------------------------------------------------

IDEM_TAKEOVER_MS = 60_000
IDEM_DEFAULT_H = 24
IDEM_MAX_H = 168
IDEM_STORED_ERRORS = {400, 403, 404, 413, 422}
_idem = {"ready": False}


def idem_ready(flag: bool = True) -> None:
    """coord.init marks the kh_idem table present."""
    _idem["ready"] = flag


def idem_retention_h(st: NsState | None) -> int:
    h = ((st.config.get("retention") or {}) if st else {}).get("idempotency_hours", IDEM_DEFAULT_H)
    try:
        h = int(h)
    except (TypeError, ValueError):
        h = IDEM_DEFAULT_H
    return max(1, min(IDEM_MAX_H, h))


def request_hash(method: str, path: str, query: str, body: bytes) -> str:
    return sha256_text(f"{method} {path}?{query}\n".encode() + (body or b""))


def _idem_claim(ns: str, principal: str, key: str, sha: str, retention_h: int) -> dict | None:
    """None = we own the key; else {replay: (status, body)} or raises in_progress / mismatch."""
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        row = {"ns": ns, "principal": principal, "idem_key": key, "request_sha256": sha, "state": "pending",
               "status_code": None, "response_json": None, "created_ms": now, "updated_ms": now}
        if sqldb.insert_new(c, "kh_idem", row):
            return None
        r = c.execute("SELECT request_sha256, state, status_code, response_json, created_ms, updated_ms "
                      "FROM kh_idem WHERE ns=? AND principal=? AND idem_key=?", (ns, principal, key)).fetchone()
        if r is None:
            if sqldb.insert_new(c, "kh_idem", row):
                return None
            raise CapError("in_progress", "this Idempotency-Key is being used", headers={"Retry-After": "1"})
        expired = r["state"] == "done" and int(r["created_ms"] or 0) < now - retention_h * 3_600_000
        stale = r["state"] == "pending" and int(r["updated_ms"] or 0) < now - IDEM_TAKEOVER_MS
        if expired or stale:
            cur = c.execute("UPDATE kh_idem SET request_sha256=?, state='pending', status_code=NULL, "
                            "response_json=NULL, created_ms=?, updated_ms=? WHERE ns=? AND principal=? "
                            "AND idem_key=? AND updated_ms=?",
                            (sha, now, now, ns, principal, key, r["updated_ms"]))
            if cur.rowcount and cur.rowcount > 0:
                return None
            raise CapError("in_progress", "this Idempotency-Key is being used", headers={"Retry-After": "1"})
        if r["state"] != "done":
            raise CapError("in_progress", "this Idempotency-Key is being used", headers={"Retry-After": "1"})
        if r["request_sha256"] != sha:
            raise CapError("idempotency_mismatch", "this Idempotency-Key was used for a different request")
        return {"status": int(r["status_code"] or 200), "body": loads(r["response_json"], None)}


def _idem_store(ns: str, principal: str, key: str, status: int, body: Any) -> None:
    with sqldb.connect() as c:
        c.execute("UPDATE kh_idem SET state='done', status_code=?, response_json=?, updated_ms=? "
                  "WHERE ns=? AND principal=? AND idem_key=?",
                  (int(status), dumps(body), sqldb.now_ms(c), ns, principal, key))


def _idem_free(ns: str, principal: str, key: str) -> None:
    with sqldb.connect() as c:
        c.execute("DELETE FROM kh_idem WHERE ns=? AND principal=? AND idem_key=? AND state='pending'",
                  (ns, principal, key))


def _find(args, kwargs, cls):
    for v in list(kwargs.values()) + list(args):
        if isinstance(v, cls):
            return v
    return None


def idempotent(fn):
    """Decorator for async /v1 POST handlers taking `request: Request` and a `Ctx` (Depends(ns_ctx)).

    With `Idempotency-Key`, the first call owns the key; a repeat with the same request hash replays the stored
    status/body (`Idempotent-Replay: true`, `kh.duplicate`); still running -> 409 in_progress; other hash -> 422
    idempotency_mismatch. Errors 400/403/404/413/422 are stored; other failures free the key.
    Handlers should return `core.reply(...)` (a dict is sent as 200).
    """

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        request = _find(args, kwargs, Request)
        ctx = _find(args, kwargs, Ctx)
        key = (request.headers.get("idempotency-key") or "").strip() if request is not None else ""
        if not key or ctx is None or not _idem["ready"]:
            return await fn(*args, **kwargs)
        if len(key) > 255:
            raise CapError("invalid", "Idempotency-Key is longer than 255 characters")
        body = await request.body()
        sha = request_hash(request.method, request.url.path, request.url.query or "", body)
        prior = await run(_idem_claim, ctx.ns, ctx.principal, key, sha, idem_retention_h(ctx.st))
        if prior is not None:
            await run(emit, ctx.ns, "kh.duplicate", {"idem_key": key, "path": request.url.path}, ctx)
            return JSONResponse(prior["body"], status_code=prior["status"], headers={"Idempotent-Replay": "true"})
        try:
            resp = await fn(*args, **kwargs)
        except CapError as e:
            if e.status in IDEM_STORED_ERRORS:
                await run(_idem_store, ctx.ns, ctx.principal, key, e.status, e.body())
            else:
                await run(_idem_free, ctx.ns, ctx.principal, key)
            raise
        except Exception:
            await run(_idem_free, ctx.ns, ctx.principal, key)
            raise
        if not isinstance(resp, Response):
            resp = JSONResponse(resp)
        status = resp.status_code
        try:
            stored = json.loads(bytes(resp.body)) if getattr(resp, "body", None) else None
        except ValueError:
            stored = None
        if status < 500:
            await run(_idem_store, ctx.ns, ctx.principal, key, status, stored)
        else:
            await run(_idem_free, ctx.ns, ctx.principal, key)
        return resp

    return wrapper


# ---------------------------------------------------------------------------
# Index columns and JSON Schema
# ---------------------------------------------------------------------------

IX_SLOTS = {"s": ["ix_s1", "ix_s2", "ix_s3", "ix_s4"], "n": ["ix_n1", "ix_n2"], "t": ["ix_t1", "ix_t2"]}
IX_STR_MAX = 256


def _walk(body: Any, dotted: str) -> Any:
    cur = body
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def ix_kind(col: str) -> str:
    return col[3]


def index_values(trow: dict | None, body: Any) -> dict:
    """{ix_col: value} for a type's index fields (dotted names walk into the body).

    s: string truncated to 256 (non-strings JSON-dumped); n: float; t: int ms. Unusable values -> None.
    """
    out: dict[str, Any] = {}
    for name, col in ((trow or {}).get("index_fields") or {}).items():
        v = _walk(body, name)
        k = ix_kind(col)
        if v is None:
            out[col] = None
        elif k == "s":
            out[col] = (v if isinstance(v, str) else canonical(v))[:IX_STR_MAX]
        elif k == "n":
            try:
                out[col] = None if isinstance(v, bool) else float(v)
            except (TypeError, ValueError):
                out[col] = None
        else:
            try:
                out[col] = None if isinstance(v, bool) else int(v)
            except (TypeError, ValueError):
                out[col] = parse_ms_quiet(v)
    return out


def parse_ms_quiet(v: Any) -> int | None:
    try:
        return parse_ms(v)
    except CapError:
        return None


def index_view(trow: dict | None, row: Any) -> dict:
    """{field: stored ix value} for responses."""
    out = {}
    for name, col in ((trow or {}).get("index_fields") or {}).items():
        try:
            out[name] = row[col]
        except (KeyError, IndexError):
            out[name] = None
    return out


_validators: dict[str, Any] = {}
MAX_SCHEMA_ERRORS = 50


def schema_check(schema: Any) -> list[str]:
    """Errors of a JSON Schema 2020-12 document (empty = compiles)."""
    try:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError
    except ImportError:  # pragma: no cover
        return [] if isinstance(schema, (dict, bool)) else ["schema must be an object"]
    if not isinstance(schema, (dict, bool)):
        return ["schema must be a JSON object"]
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        return [e.message]
    return []


def validate(schema: Any, body: Any) -> list[dict]:
    """[{pointer, message}] (<= 50) of body against a JSON Schema 2020-12 (None schema = valid)."""
    if schema is None:
        return []
    try:
        from jsonschema import Draft202012Validator
    except ImportError:  # pragma: no cover
        return []
    key = sha256_json(schema)
    v = _validators.get(key)
    if v is None:
        v = Draft202012Validator(schema)
        _validators[key] = v
    out = []
    for e in v.iter_errors(body):
        ptr = "".join(f"/{p}" for p in e.absolute_path)
        out.append({"pointer": ptr or "/", "message": e.message})
        if len(out) >= MAX_SCHEMA_ERRORS:
            break
    return out


# ---------------------------------------------------------------------------
# Admin persons (registry / principals / lifecycle)
# ---------------------------------------------------------------------------


def admin_person(projects: Iterable[str] | None = None, row: dict | None = None) -> Caller:
    """Who may manage a namespace: KH maintainer, or a person owning every listed project (and, for an
    existing ns, being its owner). Service tokens refused. Auth off -> local user."""
    auth = _auth()
    p = auth.current()
    if p is None:
        if auth.enabled():
            raise CapError("unauthorized", "sign-in required")
        return Caller("local", ADMIN, "local", None, ["*"], None)
    if p.ns:
        raise CapError("forbidden", "service tokens cannot manage namespaces")
    if p.is_maintainer:
        return Caller(p.email, ADMIN, "person", None, ["*"], p.email)
    if row is not None and (row.get("owner_email") or "").lower() != (p.email or "").lower():
        raise CapError("forbidden", "only the namespace owner or a KH maintainer may manage it")
    missing = [x for x in (projects or []) if not p.is_owner(x)]
    if missing:
        raise CapError("forbidden", f"you must own every listed project (not owner of: {', '.join(missing)})")
    return Caller(p.email, ADMIN, "person", None, person_projects(p), p.email)


def is_maintainer() -> bool:
    auth = _auth()
    p = auth.current()
    if p is None:
        return not auth.enabled()
    return bool(p.is_maintainer) and not p.ns
