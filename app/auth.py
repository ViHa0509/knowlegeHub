"""Auth gate (spec §2.1-2.7, §2.13-2.15).

Design rule: KH keeps NO permission table and holds NO credential to the permission store. It verifies the
token (signature, issuer, audience, expiry) and asks per request "is the permission this route needs in
`kh.all_projects` or in `kh.projects[<project this request touches>]`?". Anything the caller says about itself
(prompt, body, header) never counts. Exceptions: personal projects (access by e-mail) and sync-key readers
(`with_key_reads`, table `sync_grants` owned by app/sync.py).

Modes (§2.2), read at call time so tests can switch them:
    open      KH_AUTH_ISSUER unset            no gate; current() is None; may() is True
    sign-in   KH_AUTH_ISSUER set              Actor -> Auth -> ApiCall middlewares decide 401/403 per route
    dev       KH_DEV_BOOTSTRAP=1 (+ token)    extra bearer `khdev.<token>` = maintainer (app/dev_bootstrap.py)
The legacy shared API key mode lives in main.py.

Contract names (rebuild contract): install(app), enabled(), current(), current_actor(), visible_projects(perm),
filter_projects(ids), may(project, perm), acting_key(), acting_visible(meta), row_visible(meta, id), decide(...),
class Principal, personal_id(email), seed_guide(encode).
"""
from __future__ import annotations

import asyncio
import contextvars
import hmac
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app import config
from app.personal import OWN_SCOPES, personal_id  # personal_id re-exported (contract)

log = logging.getLogger(__name__)

__all__ = ["install", "enabled", "current", "current_actor", "current_token", "visible_projects",
           "filter_projects", "may", "acting_key", "acting_visible", "row_visible", "decide", "Principal",
           "personal_id", "ActorMiddleware", "AuthMiddleware", "ApiCallMiddleware", "ACTOR_RE", "match_rule",
           "rules", "verify", "principal_of", "acting_email", "with_key_reads", "relay", "RegistryRefused",
           "seed_guide", "LEVEL_PERMISSIONS", "PROJECT_PERMISSIONS", "KH_PERMISSIONS"]

ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
ALGORITHMS = ["RS256", "PS256", "ES256"]
LEEWAY_S = 30
JWKS_MAX_AGE_S = 600
HELP = "/auth/help"

# §2.6 permission model (data lives in KC; these mirror catalog.json for the dev principal and tests)
PROJECT_PERMISSIONS = ("project.read", "project.write", "project.log", "project.members.manage", "project.delete")
KH_PERMISSIONS = ("project.create", "kh.users.manage", "kh.users.invite", "kh.maintainers.manage")
LEVEL_PERMISSIONS = {
    "owner": ["project.read", "project.write", "project.log", "project.members.manage", "project.delete"],
    "editable": ["project.read", "project.write", "project.log"],
    "readonly": ["project.read"],
}

_current: contextvars.ContextVar["Principal | None"] = contextvars.ContextVar("kh_principal", default=None)
_actor: contextvars.ContextVar[str | None] = contextvars.ContextVar("kh_actor", default=None)
_token: contextvars.ContextVar[str | None] = contextvars.ContextVar("kh_token", default=None)


# ---------------------------------------------------------------------------
# Settings (read at call time)
# ---------------------------------------------------------------------------


def enabled() -> bool:
    """True when sign-in is on (KH_AUTH_ISSUER set)."""
    return bool(config.AUTH_ISSUER)


def issuer() -> str | None:
    return (config.AUTH_ISSUER or "").rstrip("/") or None


def audience() -> str:
    return config.AUTH_AUDIENCE or "kh-api"


def ns_client_prefix() -> str:
    return config._env("KH_NS_CLIENT_PREFIX", "kh-ns-") or "kh-ns-"


def principal_issuer() -> str:
    return config._env("KH_PRINCIPAL_ISSUER", "kh-principals") or "kh-principals"


# ---------------------------------------------------------------------------
# Principal (§2.4)
# ---------------------------------------------------------------------------


@dataclass
class Principal:
    email: str
    role: str = "member"
    kh_permissions: set = field(default_factory=set)
    every_project: set = field(default_factory=set)
    projects: dict = field(default_factory=dict)
    ns: str | None = None
    svc_role: str | None = None
    on_behalf_of: str | None = None
    principal_id: str | None = None
    acting_user: str | None = None

    @property
    def is_maintainer(self) -> bool:
        return self.role == "maintainer"

    @property
    def is_technical(self) -> bool:
        return "kh.act_for_user" in self.kh_permissions

    @property
    def is_person(self) -> bool:
        return not self.ns

    @property
    def personal_project(self) -> str:
        return personal_id(self.email)

    def is_owner(self, project: str) -> bool:
        return "project.members.manage" in self.projects.get(project, set())

    def can(self, project: str | None, perm: str) -> bool:
        if not project:
            return False
        if project == self.personal_project and not self.ns:
            return True
        return perm in self.every_project or perm in self.projects.get(project, set())

    def has(self, perm: str) -> bool:
        return perm in self.kh_permissions

    def visible_projects(self, perm: str = "project.read") -> set | None:
        if perm in self.every_project:
            return None
        out = {p for p, perms in self.projects.items() if perm in perms}
        if not self.ns:
            out.add(self.personal_project)
        return out

    @classmethod
    def from_claims(cls, claims: dict) -> "Principal | None":
        kh = claims.get("kh")
        client = claims.get("azp") or claims.get("client_id") or ""
        m = re.match(r"^" + re.escape(ns_client_prefix()) + r"([a-z][a-z0-9_]{2,31})$", str(client))
        if m and not isinstance(kh, dict):
            ns = m.group(1)
            return cls(email=f"client:{ns}", role="member", ns=ns, svc_role="admin", principal_id="client")
        if not isinstance(kh, dict):
            return None
        email = str(kh.get("email") or claims.get("email") or "").strip().lower()
        role = kh.get("role")
        if not email or role not in ("maintainer", "member"):
            return None
        projects: dict[str, set] = {}
        raw = kh.get("projects") or {}
        if isinstance(raw, dict):
            for pid, perms in raw.items():
                projects[str(pid)] = set(perms or [])
        return cls(email=email, role=role, kh_permissions=set(kh.get("permissions") or []),
                   every_project=set(kh.get("all_projects") or []), projects=projects)


# ---------------------------------------------------------------------------
# Module helpers (§2.4)
# ---------------------------------------------------------------------------


def current() -> Principal | None:
    return _current.get()


def current_actor() -> str | None:
    return _actor.get()


def current_token() -> str | None:
    """Raw bearer of this request (only for relaying to KC)."""
    return _token.get()


def visible_projects(perm: str = "project.read") -> set | None:
    p = current()
    return None if p is None else p.visible_projects(perm)


def filter_projects(ids: Iterable[str]) -> list[str]:
    vis = visible_projects()
    return [i for i in ids if vis is None or i in vis]


def may(project: str | None, perm: str) -> bool:
    p = current()
    if p is None:
        return True
    return bool(project) and p.can(project, perm)


def acting_key() -> str | None:
    """The real person behind the caller (acting user of a technical user, else own e-mail)."""
    p = current()
    if p is None:
        return None
    return p.acting_user or p.email


def acting_visible(meta: dict | None) -> bool:
    """A row carrying `acting_user` is visible only to that person, the technical user acting for them, or a
    maintainer."""
    p = current()
    if p is None:
        return True
    au = (meta or {}).get("acting_user")
    if not au:
        return True
    return p.is_maintainer or au == p.email or au == p.acting_user


def row_visible(meta: dict | None, row_id: str | None = None) -> bool:
    p = current()
    if p is None:
        return True
    meta = meta or {}
    if not acting_visible(meta):
        return False
    if meta.get("kind") == "project-registry" or (row_id or "").startswith("project_registry__"):
        pid = meta.get("project_id") or meta.get("subject_key") or (row_id or "")[len("project_registry__"):]
        return may(pid, "project.read")
    return True


# ---------------------------------------------------------------------------
# Rules (§2.5)
# ---------------------------------------------------------------------------

RULES_PATH = Path(__file__).resolve().parent / "auth_rules.json"
_rules_cache: list[dict] | None = None
_rules_lock = threading.Lock()


def _compile(template: str) -> re.Pattern:
    out, pos = [], 0
    for m in re.finditer(r"\{([A-Za-z_][A-Za-z0-9_]*)(:path)?\}", template):
        out.append(re.escape(template[pos:m.start()]))
        out.append(f"(?P<{m.group(1)}>.*)" if m.group(2) else f"(?P<{m.group(1)}>[^/]+)")
        pos = m.end()
    out.append(re.escape(template[pos:]))
    return re.compile("^" + "".join(out) + "$")


def rules() -> list[dict]:
    global _rules_cache
    if _rules_cache is None:
        with _rules_lock:
            if _rules_cache is None:
                data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
                out = []
                for r in data.get("routes") or []:
                    method, _, template = str(r["route"]).partition(" ")
                    rr = dict(r)
                    rr["method"] = method.upper()
                    rr["template"] = template
                    rr["_re"] = _compile(template)
                    out.append(rr)
                _rules_cache = out
    return _rules_cache


def match_rule(method: str, path: str) -> tuple[dict | None, dict]:
    """First rule whose method (or `*`) and template match. Returns (rule, path_params)."""
    method = (method or "").upper()
    for r in rules():
        if r["method"] not in ("*", method):
            continue
        m = r["_re"].match(path)
        if m:
            return r, m.groupdict()
    return None, {}


def _ref(ref: str, path_params: dict, query: dict, body: Any):
    src, _, rest = ref.partition(".")
    if src == "path":
        return (path_params or {}).get(rest)
    if src == "query":
        return (query or {}).get(rest)
    if src == "body":
        cur = body
        for part in rest.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        return cur
    return None


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or isinstance(v, (dict, list))


def rule_project(rule: dict, principal: Principal | None, path_params: dict | None, query: dict | None,
                 body: Any) -> tuple[str | None, str, bool]:
    """(project, permission, from_default) the rule resolves for this request (§2.5 step 6)."""
    from app import vector_store as vs

    perm = rule.get("permission") or "project.read"
    project = None
    for ref in rule.get("project_from") or []:
        v = _ref(ref, path_params or {}, query or {}, body)
        if not _blank(v):
            project = str(v).strip()
            break
    scope = None
    if isinstance(body, dict) and isinstance(body.get("scope"), str):
        scope = body.get("scope")
    elif query and query.get("scope"):
        scope = query.get("scope")
    from_default = False
    if project is None and principal is not None and not principal.ns and scope and (
            scope == config.PERSONAL_SCOPE or (perm == "project.write" and scope in OWN_SCOPES)):
        project = principal.personal_project
    if rule.get("log_permission") and isinstance(body, dict) and body.get("scope") == config.HOTLOG_SCOPE:
        perm = rule["log_permission"]
    if project is None and rule.get("default_project"):
        project = rule["default_project"]
        from_default = True
    if project is not None:
        project = vs.normalize_project(project)
    return project, perm, from_default


def decide(rule: dict | None = None, principal: Principal | None = None, path_params: dict | None = None,
           query: dict | None = None, body: Any = None) -> str | None:
    """§2.5 decide(): a refusal reason, or None when allowed. No principal (open mode) = allowed."""
    if principal is None:
        return None
    kind = (rule or {}).get("kind")
    template = (rule or {}).get("template") or str((rule or {}).get("route") or "").partition(" ")[2]
    if principal.ns:
        if rule is None or kind not in ("ns", "authenticated", "public") or not template.startswith("/v1/"):
            return "service tokens may call /v1 routes only"
        if kind == "ns" and (path_params or {}).get("ns") != principal.ns:
            return "this token belongs to another namespace"
        return None
    if rule is None:
        return None if principal.is_maintainer else "route not open to members"
    if kind in ("ns", "public", "authenticated"):
        return None
    if kind == "maintainer":
        return None if principal.is_maintainer else "maintainers only"
    if kind == "kh":
        perm = rule.get("permission") or ""
        return None if (principal.has(perm) or principal.is_maintainer) else f"missing permission {perm}"
    if kind == "project":
        project, perm, from_default = rule_project(rule, principal, path_params, query, body)
        if not project:
            return None if principal.is_maintainer else "a project must be named"
        if principal.can(project, perm):
            return None
        msg = f"missing permission {perm} on this project"
        if from_default:
            refs = [r for r in (rule.get("project_from") or []) if r.startswith(("body.", "query."))]
            where = " or ".join(refs) if refs else "body.project or query.project"
            msg += (f" (the request names no project, so it touched '{project}'; name it in {where})")
        return msg
    return None if principal.is_maintainer else "route not open to members"


# ---------------------------------------------------------------------------
# Token validation (§2.3)
# ---------------------------------------------------------------------------


class AuthUnavailable(RuntimeError):
    """Verification infrastructure failed (JWKS unreachable, ...) -> 503."""


_jwks_cache: dict[str, dict] = {}  # jwks url -> {"keys": {kid: key}, "at": t}
_jwks_lock = threading.Lock()


def _fetch_jwks(url: str) -> dict:
    """GET the JWKS document (tests monkeypatch this)."""
    import requests

    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json()


def jwks_url(iss: str | None = None) -> str:
    return (iss or issuer() or "").rstrip("/") + "/protocol/openid-connect/certs"


def reset_caches() -> None:
    _jwks_cache.clear()
    global _acting_cache
    _acting_cache = None
    _key_reads_seen.clear()


def _signing_key(kid: str, url: str):
    import jwt

    entry = _jwks_cache.get(url)
    fresh = entry is not None and time.time() - entry["at"] < JWKS_MAX_AGE_S
    if entry is None or not fresh or kid not in entry["keys"]:
        try:
            doc = _fetch_jwks(url)
        except Exception as e:  # noqa: BLE001
            if entry is not None and kid in entry["keys"]:
                return entry["keys"][kid]
            raise AuthUnavailable(f"JWKS fetch failed: {e}") from e
        keys = {}
        for k in (doc or {}).get("keys") or []:
            if k.get("use") != "sig" or not k.get("kid"):
                continue
            try:
                keys[k["kid"]] = jwt.PyJWK(k).key
            except Exception as e:  # noqa: BLE001
                log.warning("JWKS key %s skipped: %s", k.get("kid"), e)
        with _jwks_lock:
            _jwks_cache[url] = entry = {"keys": keys, "at": time.time()}
    key = entry["keys"].get(kid)
    if key is None:
        raise ValueError("unknown signing key")
    return key


def verify(token: str, iss: str | None = None, aud: str | None = None, jwks_uri: str | None = None) -> dict:
    """Verify a person token. ValueError(reason) -> 401; AuthUnavailable -> 503."""
    import jwt

    iss = (iss or issuer() or "").rstrip("/")
    try:
        header = jwt.get_unverified_header(token)
    except Exception:  # noqa: BLE001
        raise ValueError("malformed token")
    kid = header.get("kid")
    if not kid:
        raise ValueError("unknown signing key")
    key = _signing_key(kid, jwks_uri or jwks_url(iss))
    try:
        return jwt.decode(token, key, algorithms=ALGORITHMS, audience=aud or audience(), issuer=iss,
                          leeway=LEEWAY_S, options={"require": ["exp", "iat", "iss", "aud"]})
    except jwt.ExpiredSignatureError:
        raise ValueError("token expired")
    except jwt.InvalidTokenError as e:
        raise ValueError(f"invalid token: {e}")


def _unverified(token: str) -> dict:
    import jwt

    try:
        return jwt.decode(token, options={"verify_signature": False})
    except Exception:  # noqa: BLE001
        raise ValueError("malformed token")


def _is_service_token(token: str) -> bool:
    try:
        return _unverified(token).get("iss") == principal_issuer()
    except ValueError:
        return False


def principal_of(token: str) -> Principal | None:
    """Bearer -> Principal (None = not a registered KH user). Raises ValueError (401) / AuthUnavailable (503)."""
    if token.startswith("khdev."):
        from app import dev_bootstrap

        p = dev_bootstrap.principal_for(token)
        if p is not None:
            return p
    if _is_service_token(token):
        # KH-signed EdDSA service token (§6.5, app/caps/principals.py); built only when §6 exists.
        try:
            from app.caps import principals as caps_principals  # type: ignore
        except Exception:  # noqa: BLE001
            caps_principals = None
        fn = getattr(caps_principals, "principal_of", None) if caps_principals else None
        if not callable(fn):
            raise ValueError("invalid token: KH service tokens are not enabled on this KH")
        return fn(token)
    claims = verify(token)
    return Principal.from_claims(claims)


# --- acting tokens (technical users) ----------------------------------------

_acting_cache: tuple[str | None, list[dict]] | None = None


def acting_issuers() -> list[dict]:
    """KH_ACTING_ISSUERS JSON list of {issuer, audience, technical_users, jwks_uri?}; broken entries skipped."""
    global _acting_cache
    raw = config._env("KH_ACTING_ISSUERS") or config.ACTING_ISSUERS_RAW
    if _acting_cache is not None and _acting_cache[0] == raw:
        return _acting_cache[1]
    out: list[dict] = []
    if raw:
        try:
            data = json.loads(raw)
        except Exception as e:  # noqa: BLE001
            log.error("KH_ACTING_ISSUERS is not JSON: %s", e)
            data = []
        for i, e in enumerate(data if isinstance(data, list) else []):
            if not isinstance(e, dict) or not e.get("issuer") or not e.get("audience") \
                    or not isinstance(e.get("technical_users"), list):
                log.error("KH_ACTING_ISSUERS entry %d skipped (needs issuer, audience, technical_users)", i)
                continue
            out.append({"issuer": str(e["issuer"]).rstrip("/"), "audience": e["audience"],
                        "technical_users": [str(u).strip().lower() for u in e["technical_users"]],
                        "jwks_uri": e.get("jwks_uri")})
    _acting_cache = (raw, out)
    return out


def acting_email(token: str, actor: Principal) -> str:
    """E-mail of the real person an acting token names. ValueError(reason) -> 401."""
    token = (token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise ValueError("empty token")
    claims_u = _unverified(token)
    iss = str(claims_u.get("iss") or "").rstrip("/")
    if iss == principal_issuer():
        raise ValueError("KH service tokens cannot name a person")
    if iss and iss == issuer():
        claims = verify(token)
        if claims.get("email_verified") is False:
            raise ValueError("e-mail not verified")
    else:
        entry = next((e for e in acting_issuers() if e["issuer"] == iss), None)
        if entry is None:
            raise ValueError("issuer not trusted")
        if actor.email.lower() not in entry["technical_users"]:
            raise ValueError("this technical user may not act for that issuer")
        claims = verify(token, iss=iss, aud=entry["audience"], jwks_uri=entry.get("jwks_uri") or jwks_url(iss))
        if claims.get("email_verified") is not True:
            raise ValueError("e-mail not verified")
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise ValueError("token carries no e-mail")
    return email


# --- sync-key readers ---------------------------------------------------------

_key_reads_seen: dict = {}


def with_key_reads(principal: Principal) -> Principal:
    """Add project.read for projects the person pulled with a verified sync key (non-maintainer persons only).

    Reads `app.sync.key_reads(email)` when §4 provides it, else the `sync_grants` table; tolerates both missing.
    """
    if principal is None or principal.ns or principal.is_maintainer:
        return principal
    pids: list[str] = []
    try:
        from app import sync as _sync  # type: ignore

        fn = getattr(_sync, "key_reads", None)
    except Exception:  # noqa: BLE001
        fn = None
    try:
        if callable(fn):
            pids = list(fn(principal.email) or [])
        else:
            from app import sqldb

            with sqldb.connect() as c:
                if sqldb.table_exists(c, "sync_grants"):
                    pids = [r[0] for r in c.execute("SELECT project FROM sync_grants WHERE email=?",
                                                    (principal.email,)).fetchall()]
    except Exception as e:  # noqa: BLE001
        log.debug("with_key_reads skipped: %s", e)
    for pid in pids:
        principal.projects.setdefault(str(pid), set()).add("project.read")
    return principal


# ---------------------------------------------------------------------------
# KC relay (§2.7): always with the caller's own bearer
# ---------------------------------------------------------------------------


class RegistryRefused(Exception):
    def __init__(self, status: int, detail: Any):
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


def relay(method: str, path: str, body: dict | None = None, timeout: float = 10.0) -> tuple[int, Any]:
    """Call `<KH_AUTH_ISSUER>/kh<path>` with the caller's token. Returns (status, json-or-text).

    Raises AuthUnavailable when KC cannot be reached (callers turn it into 502). Tests monkeypatch this.
    """
    import requests

    url = (issuer() or "") + "/kh" + path
    headers = {"Accept": "application/json"}
    tok = current_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    try:
        r = requests.request(method, url, json=body, headers=headers, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        raise AuthUnavailable(f"identity service unreachable: {e}") from e
    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        data = r.text
    return r.status_code, data


def kc_error(data: Any) -> str:
    if isinstance(data, dict):
        return str(data.get("error") or data.get("detail") or data.get("message") or data)
    return str(data or "")


# ---------------------------------------------------------------------------
# Middlewares
# ---------------------------------------------------------------------------


def _header(scope, name: bytes) -> str | None:
    for k, v in scope.get("headers") or []:
        if k == name:
            return v.decode("latin-1")
    return None


class ActorMiddleware:
    """Reads `X-KH-Actor` (regex-validated; malformed = none) into a context var, with or without sign-in."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        val = (_header(scope, b"x-kh-actor") or "").strip()
        actor = val if val and ACTOR_RE.match(val) else None
        tok = _actor.set(actor)
        try:
            await self.app(scope, receive, send)
        finally:
            _actor.reset(tok)


async def _json(send, status: int, body: dict, headers: dict | None = None):
    # Same bytes as Starlette's JSONResponse (and the original KH): compact separators, UTF-8 kept.
    raw = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]
    for k, v in (headers or {}).items():
        hdrs.append((k.lower().encode(), str(v).encode()))
    await send({"type": "http.response.start", "status": status, "headers": hdrs})
    await send({"type": "http.response.body", "body": raw})


def _query_dict(scope) -> dict:
    from urllib.parse import parse_qsl

    qs = (scope.get("query_string") or b"").decode("latin-1")
    out: dict = {}
    for k, v in parse_qsl(qs, keep_blank_values=True):
        out.setdefault(k, v)
    return out


class AuthMiddleware:
    """§2.3: verify the bearer, build the Principal, decide 401/403 per route, replay the buffered body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not enabled():
            return await self.app(scope, receive, send)
        method = scope.get("method", "GET").upper()
        path = scope.get("path") or "/"
        rule, path_params = match_rule(method, path)
        if method == "OPTIONS" or (rule is not None and rule.get("kind") == "public"):
            return await self.app(scope, receive, send)
        authz = (_header(scope, b"authorization") or "").strip()
        token = authz[7:].strip() if authz[:7].lower() == "bearer " else ""
        if not token:
            return await _json(send, 401, {"detail": "sign-in required", "help": HELP})
        try:
            principal = await asyncio.to_thread(principal_of, token)
        except ValueError as e:
            return await _json(send, 401, {"detail": str(e), "help": HELP})
        except Exception as e:  # noqa: BLE001
            log.warning("token verification unavailable: %s", e)
            return await _json(send, 503, {"detail": "sign-in service unavailable"})
        if principal is None:
            return await _json(send, 403, {"detail": "not a registered KH user"})
        acting = _header(scope, b"x-kh-acting-token")
        if acting and acting.strip() and not principal.ns:
            if not principal.is_technical:
                log.info("x-kh-acting-token ignored: %s does not hold kh.act_for_user", principal.email)
            else:
                try:
                    email = await asyncio.to_thread(acting_email, acting, principal)
                except ValueError as e:
                    return await _json(send, 401, {"detail": f"x-kh-acting-token: {e}"})
                except Exception as e:  # noqa: BLE001
                    log.warning("acting token verification unavailable: %s", e)
                    return await _json(send, 503, {"detail": "sign-in service unavailable"})
                if email != principal.email:
                    principal.acting_user = email
        if not principal.ns:
            with_key_reads(principal)
        # buffer the whole body
        chunks = []
        more = True
        while more:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            chunks.append(msg.get("body", b""))
            more = msg.get("more_body", False)
        raw = b"".join(chunks)
        body: Any = None
        ctype = (_header(scope, b"content-type") or "").lower()
        if raw and "json" in ctype:
            try:
                body = json.loads(raw)
            except Exception:  # noqa: BLE001
                body = None
        query = _query_dict(scope)
        actor = current_actor()
        if principal.is_person and principal.email and not principal.email.startswith("client:"):
            try:
                from app import session_project

                session_project.touch(principal.personal_project, actor)
                if method == "POST" and path == "/vector/add" and isinstance(body, dict):
                    if session_project.apply_to_log_write(principal, actor, body):
                        raw = json.dumps(body).encode()
                        scope = dict(scope)
                        scope["headers"] = [(k, v) for k, v in scope.get("headers") or []
                                            if k != b"content-length"] + [(b"content-length",
                                                                           str(len(raw)).encode())]
            except Exception as e:  # noqa: BLE001
                log.warning("session binding hook failed: %s", e)
        reason = decide(rule, principal, path_params, query, body)
        if reason:
            return await _json(send, 403, {"detail": reason})
        if principal.is_person and not principal.email.startswith("client:"):
            try:
                from app import messages

                messages.note_user(principal.email)
            except Exception as e:  # noqa: BLE001
                log.debug("note_user failed: %s", e)
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": raw, "more_body": False}
            return await receive()

        t1 = _current.set(principal)
        t2 = _token.set(token)
        try:
            await self.app(scope, replay, send)
        finally:
            _current.reset(t1)
            _token.reset(t2)


_API_SKIP = ("/health", "/queue/", "/favicon.ico", "/static/")


class ApiCallMiddleware:
    """Emits `kh.api.call {method, path, status, ms, hook_type?}` (sign-in on; innermost, sees the principal)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not enabled():
            return await self.app(scope, receive, send)
        path = scope.get("path") or "/"
        if path.startswith(_API_SKIP):
            return await self.app(scope, receive, send)
        t0 = time.perf_counter()
        status = {"code": 500}

        async def send_wrap(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]
            await send(msg)

        try:
            await self.app(scope, receive, send_wrap)
        finally:
            try:
                from app.eventing.emit import emit

                ms = round((time.perf_counter() - t0) * 1000, 1)
                body = {"method": scope.get("method"), "path": path, "status": status["code"], "ms": ms}
                ht = _query_dict(scope).get("hook_type")
                if ht:
                    body["hook_type"] = ht
                p = current()
                emit("kh.api.call", body, email=getattr(p, "email", None), actor=current_actor(), ix2=path, n1=ms)
            except Exception:  # noqa: BLE001
                pass


def install(app) -> None:
    """Middlewares are added in order ApiCall, Auth, Actor, so they run Actor -> Auth -> ApiCall.

    The Auth and ApiCall layers check `enabled()` per request (open mode passes straight through), so a
    test or a restart can switch sign-in without rebuilding the app.
    """
    app.add_middleware(ApiCallMiddleware)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(ActorMiddleware)


# ---------------------------------------------------------------------------
# /auth/help guide seed (called by main.py at startup when sign-in is on)
# ---------------------------------------------------------------------------


def seed_guide(encode) -> bool:
    """Store the /auth/help guide row from seeds/auth_help.seed.txt if missing (never overwritten)."""
    from app import auth_routes

    return auth_routes.seed_guide(encode)
