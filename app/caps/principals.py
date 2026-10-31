"""principals (spec §6.5): service principals (agents) with KH-signed EdDSA tokens, the namespace client,
`/v1/token`, `/v1/me`. Agents are not Keycloak users: they are rows authenticated with KH-signed JWTs.

Grants never widen: principal.projects ∩ ns.projects ∩ projects of `on_behalf_of` (snapshot `kh_owner_grant`).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from typing import Any

from fastapi import Depends, FastAPI, Query, Request

from app import config, sqldb
from app.caps import core
from app.caps.core import CapError, Ctx

log = logging.getLogger(__name__)

NAME = "principals"
VERSION = "1.0.0"
STATUS = "beta"
DEFAULT_ON = True
AUDIENCE = "kh-v1"
TOKEN_TYP = "kh+jwt"
LEEWAY_S = 30
PBKDF2_ITER = 120000
LIST_MAX = 2000
PID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
STATES = ("active", "suspended", "revoked")
GRANT_REFRESH_S = 60
TABLES = ["kh_principal", "kh_ns_client", "kh_owner_grant"]
LOCAL_TABLES: list[str] = []


def token_s() -> int:
    return config._int("KH_PRINCIPAL_TOKEN_S", 900)


def principals_max() -> int:
    return config._int("KH_PRINCIPALS_MAX", 2000)


def issuer() -> str:
    return config._env("KH_PRINCIPAL_ISSUER", "kh-principals") or "kh-principals"


def client_prefix() -> str:
    return config._env("KH_NS_CLIENT_PREFIX", "kh-ns-") or "kh-ns-"


def provisioner() -> str:
    return (config._env("KH_NS_PROVISIONER", "local") or "local").lower()


LIMITS = {"token_s": 900, "principals_per_ns": 2000, "list": LIST_MAX, "verify_leeway_s": LEEWAY_S}


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _m1(c) -> None:
    sqldb.create_table(
        c, "kh_principal",
        "CREATE TABLE IF NOT EXISTS kh_principal (ns TEXT NOT NULL, principal_id TEXT NOT NULL, role TEXT NOT NULL, "
        "on_behalf_of TEXT, projects_json TEXT, state TEXT NOT NULL, created_by TEXT, created_ms INTEGER, "
        "expires_ms INTEGER, rev INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (ns, principal_id))",
        "CREATE COLUMN TABLE kh_principal (ns NVARCHAR(64) NOT NULL, principal_id NVARCHAR(64) NOT NULL, "
        "role NVARCHAR(32) NOT NULL, on_behalf_of NVARCHAR(256), projects_json NCLOB, state NVARCHAR(16) NOT NULL, "
        "created_by NVARCHAR(256), created_ms BIGINT, expires_ms BIGINT, rev BIGINT NOT NULL, "
        "PRIMARY KEY (ns, principal_id))")
    sqldb.create_table(
        c, "kh_ns_client",
        "CREATE TABLE IF NOT EXISTS kh_ns_client (ns TEXT PRIMARY KEY, client_id TEXT NOT NULL, provisioner TEXT, "
        "secret_hash TEXT, state TEXT NOT NULL, created_ms INTEGER, rotated_ms INTEGER, "
        "rev INTEGER NOT NULL DEFAULT 1)",
        "CREATE COLUMN TABLE kh_ns_client (ns NVARCHAR(64) PRIMARY KEY, client_id NVARCHAR(128) NOT NULL, "
        "provisioner NVARCHAR(16), secret_hash NVARCHAR(256), state NVARCHAR(16) NOT NULL, created_ms BIGINT, "
        "rotated_ms BIGINT, rev BIGINT NOT NULL)")
    sqldb.create_index(c, "kh_ns_client_id", "kh_ns_client", ["client_id"], unique=True)
    sqldb.create_table(
        c, "kh_owner_grant",
        "CREATE TABLE IF NOT EXISTS kh_owner_grant (email TEXT PRIMARY KEY, projects_json TEXT, source TEXT, "
        "refreshed_ms INTEGER)",
        "CREATE COLUMN TABLE kh_owner_grant (email NVARCHAR(256) PRIMARY KEY, projects_json NCLOB, "
        "source NVARCHAR(16), refreshed_ms BIGINT)")


def init(c) -> None:
    sqldb.migrate(c, NAME, [(1, _m1)])
    if _resolver not in core.CALLER_RESOLVERS:
        core.CALLER_RESOLVERS.insert(0, _resolver)
    if _on_state not in core.ON_STATE:
        core.ON_STATE.append(_on_state)
    if _on_attach not in core.ON_ATTACH:
        core.ON_ATTACH.append(_on_attach)


# ---------------------------------------------------------------------------
# Signing keys (Ed25519)
# ---------------------------------------------------------------------------

_keys: dict = {}
_keys_lock = threading.Lock()


def _pub_pem(pub) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return pub.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def _kid_of(pub) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "ed-" + hashlib.sha256(raw).hexdigest()[:16]


def keys() -> dict:
    """{"kid", "private", "public": {kid: public_key}} (cached per env value)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    pem = config._env("KH_PRINCIPAL_SIGNING_KEY") or ""
    old = config._env("KH_PRINCIPAL_OLD_KEYS") or ""
    kid_env = config._env("KH_PRINCIPAL_KID") or ""
    sig = (pem, old, kid_env)
    with _keys_lock:
        if _keys.get("sig") == sig:
            return _keys
        if pem:
            priv = serialization.load_pem_private_key(pem.replace("\\n", "\n").encode(), password=None)
        else:
            priv = _keys.get("ephemeral") or Ed25519PrivateKey.generate()
            _keys["ephemeral"] = priv
            log.warning("KH_PRINCIPAL_SIGNING_KEY unset: ephemeral signing key (single instance only)")
        pub = priv.public_key()
        kid = kid_env or _kid_of(pub)
        public = {kid: pub}
        if old:
            try:
                data = json.loads(old)
            except ValueError:
                data = []
                log.error("KH_PRINCIPAL_OLD_KEYS is not JSON")
            items = data.items() if isinstance(data, dict) else [(None, x) for x in (data or [])]
            for k, v in items:
                try:
                    p = serialization.load_pem_public_key(str(v).replace("\\n", "\n").encode())
                    public[k or _kid_of(p)] = p
                except Exception as e:  # noqa: BLE001
                    log.error("old principal key skipped: %s", e)
        _keys.update({"sig": sig, "kid": kid, "private": priv, "public": public})
        return _keys


def issue(ns: str, pid: str, role: str, obo: str | None) -> tuple[str, int]:
    """KH-signed EdDSA token -> (token, exp seconds)."""
    import jwt

    k = keys()
    now = int(time.time())
    exp = now + token_s()
    claims = {"iss": issuer(), "aud": AUDIENCE, "iat": now, "exp": exp, "sub": f"{ns}:{pid}", "ns": ns,
              "pid": pid, "role": role, "obo": obo, "jti": secrets.token_hex(8)}
    tok = jwt.encode(claims, k["private"], algorithm="EdDSA", headers={"kid": k["kid"], "typ": TOKEN_TYP})
    return tok, exp


def verify(token: str) -> dict:
    import jwt

    k = keys()
    try:
        head = jwt.get_unverified_header(token)
    except Exception:  # noqa: BLE001
        raise ValueError("invalid token: malformed")
    pub = k["public"].get(head.get("kid"))
    if pub is None:
        raise ValueError("invalid token: unknown signing key")
    try:
        return jwt.decode(token, pub, algorithms=["EdDSA"], audience=AUDIENCE, issuer=issuer(), leeway=LEEWAY_S)
    except jwt.ExpiredSignatureError:
        raise ValueError("token expired")
    except jwt.PyJWTError as e:
        raise ValueError(f"invalid token: {e}")


# ---------------------------------------------------------------------------
# State caches (revocation latency <= KH_NS_CACHE_S)
# ---------------------------------------------------------------------------

_pcache: dict[tuple, tuple[float, Any]] = {}


def _cached(key: tuple, loader):
    t = time.monotonic()
    hit = _pcache.get(key)
    if hit is not None and t - hit[0] < core.cache_s():
        return hit[1]
    v = loader()
    _pcache[key] = (t, v)
    return v


def invalidate(ns: str | None = None) -> None:
    if ns is None:
        _pcache.clear()
    else:
        for k in [k for k in _pcache if k[1] == ns]:
            _pcache.pop(k, None)


def _load_principal(ns: str, pid: str) -> dict | None:
    with sqldb.connect() as c:
        r = c.execute("SELECT * FROM kh_principal WHERE ns=? AND principal_id=?", (ns, pid)).fetchone()
    return _decode(r) if r else None


def _load_client(ns: str) -> dict | None:
    with sqldb.connect() as c:
        r = c.execute("SELECT * FROM kh_ns_client WHERE ns=?", (ns,)).fetchone()
    return {k: r[k] for k in r.keys()} if r else None


def _decode(r) -> dict:
    d = {k: r[k] for k in r.keys()}
    d["projects"] = core.loads(d.pop("projects_json", None), None)
    return d


def principal_of(token: str):
    """KH service token -> auth.Principal. ValueError -> 401."""
    from app.auth import Principal

    claims = verify(token)
    ns, pid, role = claims.get("ns"), claims.get("pid"), claims.get("role")
    if not ns or not pid or not role:
        raise ValueError("invalid token: missing claims")
    if pid == "client":
        cl = _cached(("client", ns), lambda: _load_client(ns))
        if cl is None or cl.get("state") != "active":
            raise ValueError("namespace client is disabled")
    else:
        row = _cached(("principal", ns, pid), lambda: _load_principal(ns, pid))
        if row is None:
            raise ValueError("unknown principal")
        if row["state"] != "active":
            raise ValueError(f"principal is {row['state']}")
        if row.get("expires_ms") and int(row["expires_ms"]) < core.now_ms():
            raise ValueError("principal expired")
        role = row["role"]
    return Principal(email=f"svc:{ns}:{pid}", role="member", ns=ns, svc_role=role,
                     on_behalf_of=claims.get("obo"), principal_id=pid)


# ---------------------------------------------------------------------------
# Owner grants (snapshot of a person's projects)
# ---------------------------------------------------------------------------


def note_person(p) -> None:
    """Refresh the person's grant snapshot from their own token (at most 1/min)."""
    email = (p.email or "").lower()
    if not email:
        return
    key = ("grant_seen", email)
    t = time.monotonic()
    last = _pcache.get(key)
    if last is not None and t - last[0] < GRANT_REFRESH_S:
        return
    _pcache[key] = (t, True)
    projects = core.person_projects(p)
    try:
        with sqldb.connect() as c:
            sqldb.upsert(c, "kh_owner_grant", ["email"], {"email": email, "projects_json": core.dumps(projects),
                                                          "source": "token", "refreshed_ms": sqldb.now_ms(c)})
    except Exception as e:  # noqa: BLE001
        log.debug("owner grant refresh failed: %s", e)
    _pcache.pop(("grant", email), None)


def owner_projects(email: str | None) -> list[str] | None:
    """Projects of an on_behalf_of person (["*"] = maintainer); None = no narrowing (local / unknown obo)."""
    if not email or email == "local":
        return None
    email = email.lower()

    def _load():
        with sqldb.connect() as c:
            r = c.execute("SELECT projects_json FROM kh_owner_grant WHERE email=?", (email,)).fetchone()
        return core.loads(r[0], []) if r else []

    return _cached(("grant", email), _load)


def effective_projects(ns_row: dict, own: list | None, obo: str | None) -> list[str]:
    """principal.projects ∩ ns.projects ∩ obo's projects (["*"] = no narrowing)."""
    nsp = list(ns_row.get("projects") or [])
    if not nsp and ns_row.get("ns") == core.PEOPLE_NS:
        base: list | None = None  # every project
    else:
        base = nsp
    sets = [s for s in (own, owner_projects(obo)) if s is not None and "*" not in s]
    if base is None:
        if not sets:
            return ["*"]
        out = set(sets[0])
        for s in sets[1:]:
            out &= set(s)
        return sorted(out)
    out = list(base)
    for s in sets:
        out = [p for p in out if p in set(s)]
    return out


def _resolver(request, row: dict):
    from app import auth

    p = auth.current()
    if p is None:
        return None
    if not p.ns:
        note_person(p)
        return None
    if p.ns != row.get("ns"):
        raise CapError("forbidden", "this token belongs to another namespace")
    if p.principal_id == "client":
        obo = p.on_behalf_of or row.get("owner_email")
        projects = effective_projects(row, None, obo)
        return core.Caller("client", p.svc_role or core.ADMIN, "client", obo, projects or [], None)
    prow = _cached(("principal", row["ns"], p.principal_id), lambda: _load_principal(row["ns"], p.principal_id))
    if prow is None or prow["state"] != "active":
        raise CapError("unauthorized", "principal is not active")
    projects = effective_projects(row, prow.get("projects"), prow.get("on_behalf_of"))
    return core.Caller(p.principal_id, prow["role"], "service", prow.get("on_behalf_of"), projects, None)


# ---------------------------------------------------------------------------
# Active cap
# ---------------------------------------------------------------------------


def _counter(ns: str) -> str:
    return f"principals:active:{ns}"


def _take_slot(c, ns: str) -> None:
    name = _counter(ns)
    cur = c.execute("UPDATE kh_counter SET n=n+1 WHERE name=? AND n < ?", (name, principals_max()))
    if cur.rowcount and cur.rowcount > 0:
        return
    if c.execute("SELECT 1 FROM kh_counter WHERE name=?", (name,)).fetchone() is None:
        if principals_max() > 0 and sqldb.insert_new(c, "kh_counter", {"name": name, "n": 1}):
            return
        cur = c.execute("UPDATE kh_counter SET n=n+1 WHERE name=? AND n < ?", (name, principals_max()))
        if cur.rowcount and cur.rowcount > 0:
            return
    raise CapError("quota", f"at most {principals_max()} active principals per namespace",
                   extra={"quota": "principals_active"}, headers={"Retry-After": "60"})


def _free_slot(c, ns: str) -> None:
    c.execute("UPDATE kh_counter SET n=n-1 WHERE name=? AND n > 0", (_counter(ns),))


# ---------------------------------------------------------------------------
# Namespace client
# ---------------------------------------------------------------------------


def hash_secret(secret: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt.encode(), PBKDF2_ITER)
    return f"pbkdf2-sha256${PBKDF2_ITER}${salt}${base64.b64encode(dk).decode()}"


def check_secret(secret: str, stored: str | None) -> bool:
    try:
        algo, it, salt, dk = (stored or "").split("$")
        if algo != "pbkdf2-sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt.encode(), int(it))
        return hmac.compare_digest(base64.b64encode(got).decode(), dk)
    except (ValueError, TypeError):
        return False


def provision_client(ns: str, row: dict, request: Request | None = None) -> dict:
    """Create or rotate the ns client. local: secret shown once; keycloak: relay to KC."""
    client_id = client_prefix() + ns
    if provisioner() == "keycloak":
        from app import auth

        try:
            status, data = auth.relay("POST", "/kh/namespaces", {"ns": ns, "client_id": client_id,
                                                                 "owner": row.get("owner_email")})
        except Exception as e:  # noqa: BLE001
            raise CapError("provisioner_unavailable", f"identity service unreachable: {e}")
        if status in (404, 405, 501) or status >= 500:
            raise CapError("provisioner_unavailable", f"identity service cannot provision clients ({status})")
        if status >= 300:
            raise CapError("invalid", f"identity service refused: {data}")
        with sqldb.connect() as c:
            now = sqldb.now_ms(c)
            old = c.execute("SELECT rev FROM kh_ns_client WHERE ns=?", (ns,)).fetchone()
            sqldb.upsert(c, "kh_ns_client", ["ns"], {"ns": ns, "client_id": client_id, "provisioner": "keycloak",
                                                     "secret_hash": None, "state": "active",
                                                     "created_ms": now, "rotated_ms": now if old else None,
                                                     "rev": (int(old[0]) + 1) if old else 1})
        invalidate(ns)
        out = {"ns": ns, "client_id": client_id, "provisioner": "keycloak", "rotated": bool(old)}
        if isinstance(data, dict) and data.get("client_secret"):
            out["client_secret"] = data["client_secret"]
        return out
    secret = secrets.token_urlsafe(32)
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        old = c.execute("SELECT rev, created_ms FROM kh_ns_client WHERE ns=?", (ns,)).fetchone()
        sqldb.upsert(c, "kh_ns_client", ["ns"], {"ns": ns, "client_id": client_id, "provisioner": "local",
                                                 "secret_hash": hash_secret(secret),
                                                 "state": "active" if row.get("state") != "detached" else "disabled",
                                                 "created_ms": old[1] if old else now,
                                                 "rotated_ms": now if old else None,
                                                 "rev": (int(old[0]) + 1) if old else 1})
    invalidate(ns)
    return {"ns": ns, "client_id": client_id, "client_secret": secret, "provisioner": "local",
            "rotated": bool(old), "token_url": "/v1/token"}


def _note_current() -> None:
    from app import auth

    p = auth.current()
    if p is not None and not p.ns:
        _pcache.pop(("grant_seen", (p.email or "").lower()), None)
        note_person(p)


def _on_attach(ns: str, row: dict, request: Request) -> dict | None:
    _note_current()
    return provision_client(ns, row, request)


def _on_state(c, ns: str, src: str, dst: str) -> None:
    if dst in ("detached", "purged"):
        c.execute("UPDATE kh_principal SET state='suspended', rev=rev+1 WHERE ns=? AND state='active'", (ns,))
        c.execute("UPDATE kh_counter SET n=0 WHERE name=?", (_counter(ns),))
        c.execute("UPDATE kh_ns_client SET state='disabled', rev=rev+1 WHERE ns=?", (ns,))
    elif src == "detached" and dst == "active":
        c.execute("UPDATE kh_ns_client SET state='active', rev=rev+1 WHERE ns=? AND state='disabled'", (ns,))
    invalidate(ns)


def client_token(client_id: str, client_secret: str) -> dict:
    if not client_id or not client_secret:
        raise CapError("unauthorized", "client_id and client_secret are required")
    with sqldb.connect() as c:
        r = c.execute("SELECT * FROM kh_ns_client WHERE client_id=?", (client_id,)).fetchone()
    if r is None or r["state"] != "active" or not check_secret(client_secret, r["secret_hash"]):
        raise CapError("unauthorized", "invalid client credentials")
    ns = r["ns"]
    st = core.ns_state(ns)
    if st is None or st.state not in ("active", "suspended"):
        raise CapError("unauthorized", "invalid client credentials")
    obo = st.row.get("owner_email")
    tok, exp = issue(ns, "client", core.ADMIN, obo)
    return {"access_token": tok, "token_type": "Bearer", "expires_in": token_s(), "expires_at": exp,
            "principal_id": "client", "role": core.ADMIN, "ns": ns}


# ---------------------------------------------------------------------------
# Principals CRUD
# ---------------------------------------------------------------------------


def view(p: dict) -> dict:
    return {"ns": p["ns"], "principal_id": p["principal_id"], "role": p["role"], "on_behalf_of": p.get("on_behalf_of"),
            "projects": p.get("projects"), "state": p["state"], "created_by": p.get("created_by"),
            "created_ms": p.get("created_ms"), "expires_ms": p.get("expires_ms"), "rev": int(p.get("rev") or 0)}


def _may_manage(ctx: Ctx) -> None:
    if not (ctx.is_admin or ctx.allows("admin", "principals")):
        raise CapError("forbidden", "only a namespace admin or an orchestrator manages principals")


def create(ctx: Ctx, body: dict) -> dict:
    _may_manage(ctx)
    role = body.get("role")
    if role not in core.ROLES:
        raise CapError("invalid", f"role must be one of {', '.join(core.ROLES)}")
    if role == "orchestrator" and not ctx.is_admin:
        raise CapError("forbidden", "only a namespace admin creates orchestrators")
    pid = body.get("principal_id") or f"p_{secrets.token_hex(6)}"
    if not isinstance(pid, str) or not PID_RE.match(pid) or pid == "client":
        raise CapError("invalid", "principal_id must be 1-64 characters of [A-Za-z0-9_.-] (not 'client')")
    ttl = core.parse_int(body.get("ttl_s"), "ttl_s", None, 60)
    service = ctx.caller.kind in ("service", "client")
    own_projects = ctx.projects()
    projects = body.get("projects")
    if projects is not None:
        if not isinstance(projects, list) or not all(isinstance(p, str) and p for p in projects):
            raise CapError("invalid", "projects must be a list of project ids")
        nsp = ctx.st.projects
        bad = [p for p in projects if nsp and p not in nsp]
        if bad:
            raise CapError("invalid", f"projects not attached to this namespace: {', '.join(bad)}")
        if service and "*" not in own_projects:
            beyond = [p for p in projects if p not in own_projects]
            if beyond:
                raise CapError("forbidden", f"you cannot grant projects you do not hold: {', '.join(beyond)}")
        projects = list(dict.fromkeys(projects))
    elif service and "*" not in own_projects:
        projects = list(own_projects)
    obo = body.get("on_behalf_of")
    if service:
        if obo is not None and obo != ctx.caller.on_behalf_of:
            raise CapError("forbidden", "a service principal can only give its own on_behalf_of")
        obo = ctx.caller.on_behalf_of
    else:
        if obo is not None and (not isinstance(obo, str) or "@" not in obo):
            raise CapError("invalid", "on_behalf_of must be an e-mail address")
        if obo and ctx.caller.kind == "person" and obo.lower() != (ctx.caller.email or "").lower() \
                and not core.is_maintainer():
            raise CapError("forbidden", "only a KH maintainer may name another person as on_behalf_of")
        obo = (obo or ctx.caller.email or ctx.row.get("owner_email") or None)
        obo = obo.lower() if obo else None
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        row = {"ns": ctx.ns, "principal_id": pid, "role": role, "on_behalf_of": obo,
               "projects_json": core.dumps(projects) if projects is not None else None, "state": "active",
               "created_by": ctx.principal, "created_ms": now, "expires_ms": now + ttl * 1000 if ttl else None,
               "rev": 1}
        if c.execute("SELECT 1 FROM kh_principal WHERE ns=? AND principal_id=?", (ctx.ns, pid)).fetchone():
            raise CapError("duplicate", f"principal '{pid}' already exists")
        _take_slot(c, ctx.ns)
        if not sqldb.insert_new(c, "kh_principal", row):
            raise CapError("duplicate", f"principal '{pid}' already exists")
        out = _decode(c.execute("SELECT * FROM kh_principal WHERE ns=? AND principal_id=?", (ctx.ns, pid)).fetchone())
    core.emit(ctx.ns, "kh.principal", {"principal_id": pid, "role": role, "state": "active"}, ctx)
    return view(out)


def get(ctx: Ctx, pid: str) -> dict:
    with sqldb.connect() as c:
        r = c.execute("SELECT * FROM kh_principal WHERE ns=? AND principal_id=?", (ctx.ns, pid)).fetchone()
    if r is None:
        raise CapError("not_found", f"principal '{pid}' not found")
    return view(_decode(r))


def list_(ctx: Ctx, role: str | None, state: str | None, limit: int) -> list[dict]:
    q, params = "SELECT * FROM kh_principal WHERE ns=?", [ctx.ns]
    roles, states = core.csv(role), core.csv(state)
    if roles:
        q += f" AND role IN ({', '.join('?' for _ in roles)})"
        params += roles
    if states:
        q += f" AND state IN ({', '.join('?' for _ in states)})"
        params += states
    q += f" ORDER BY created_ms, principal_id LIMIT {int(limit)}"
    with sqldb.connect() as c:
        return [view(_decode(r)) for r in c.execute(q, params).fetchall()]


def patch(ctx: Ctx, pid: str, body: dict, expected: int | None) -> dict:
    _may_manage(ctx)
    new = body.get("state")
    if new not in STATES:
        raise CapError("invalid", f"state must be one of {', '.join(STATES)}")
    with sqldb.connect() as c:
        r = c.execute("SELECT * FROM kh_principal WHERE ns=? AND principal_id=?", (ctx.ns, pid)).fetchone()
        if r is None:
            raise CapError("not_found", f"principal '{pid}' not found")
        cur = _decode(r)
        if cur["role"] == "orchestrator" and not ctx.is_admin:
            raise CapError("forbidden", "only a namespace admin manages orchestrators")
        if cur["state"] == new:
            return view(cur)
        if cur["state"] == "revoked":
            raise CapError("invalid_transition", "revoked is terminal", extra={"from": "revoked", "to": new})
        if new == "active":
            _take_slot(c, ctx.ns)
        elif cur["state"] == "active":
            _free_slot(c, ctx.ns)
        core.cas_update(c, "kh_principal", {"ns": ctx.ns, "principal_id": pid}, {"state": new},
                        expected if expected is not None else int(cur["rev"]), ns=ctx.ns, what="principal")
        out = _decode(c.execute("SELECT * FROM kh_principal WHERE ns=? AND principal_id=?", (ctx.ns, pid)).fetchone())
    invalidate(ctx.ns)
    core.emit(ctx.ns, "kh.principal", {"principal_id": pid, "role": out["role"], "state": new}, ctx)
    return view(out)


def token_for(ctx: Ctx, pid: str) -> dict:
    _may_manage(ctx)
    p = get(ctx, pid)
    if p["state"] != "active":
        raise CapError("invalid_transition", f"principal '{pid}' is {p['state']}")
    if p.get("expires_ms") and int(p["expires_ms"]) < core.now_ms():
        raise CapError("invalid_transition", f"principal '{pid}' expired")
    if p["role"] == "orchestrator" and not ctx.is_admin:
        raise CapError("forbidden", "only a namespace admin issues orchestrator tokens")
    tok, exp = issue(ctx.ns, pid, p["role"], p.get("on_behalf_of"))
    return {"access_token": tok, "token_type": "Bearer", "expires_in": token_s(), "expires_at": exp,
            "principal_id": pid, "role": p["role"]}


def me(request: Request) -> dict:
    from app import auth

    p = auth.current()
    if p is None:
        return {"kind": "local", "principal": "local", "role": core.ADMIN, "ns": None, "on_behalf_of": None,
                "projects": ["*"]}
    if p.ns:
        st = core.ns_state(p.ns)
        if st is None:
            raise CapError("ns_not_active", f"namespace '{p.ns}' is not active")
        caller = _resolver(request, st.row)
        return {"kind": caller.kind, "principal": caller.principal, "role": caller.role, "ns": p.ns,
                "on_behalf_of": caller.on_behalf_of, "projects": caller.projects}
    note_person(p)
    return {"kind": "person", "principal": p.email, "email": p.email,
            "role": "maintainer" if p.is_maintainer else "member", "ns": None, "on_behalf_of": None,
            "projects": core.person_projects(p)}


# ---------------------------------------------------------------------------
# Cron: owner grants from Keycloak
# ---------------------------------------------------------------------------


def refresh_owner_grants() -> int:
    """Refresh kh_owner_grant via KC `GET {issuer}/kh/users/{email}/projects` (client credentials)."""
    cid, secret = config._env("KH_KC_CLIENT_ID"), config._env("KH_KC_CLIENT_SECRET")
    iss = config._env("KH_AUTH_ISSUER")
    if not (cid and secret and iss):
        return 0
    import requests

    iss = iss.rstrip("/")
    try:
        t = requests.post(f"{iss}/protocol/openid-connect/token",
                          data={"grant_type": "client_credentials", "client_id": cid, "client_secret": secret},
                          timeout=10)
        t.raise_for_status()
        bearer = t.json()["access_token"]
    except Exception as e:  # noqa: BLE001
        log.warning("owner grant refresh: no KC token: %s", e)
        return 0
    with sqldb.connect() as c:
        emails = {str(r[0]).lower() for r in c.execute("SELECT email FROM kh_owner_grant").fetchall()}
        emails |= {str(r[0]).lower() for r in c.execute(
            "SELECT DISTINCT on_behalf_of FROM kh_principal WHERE state='active' AND on_behalf_of IS NOT NULL"
        ).fetchall()}
    n = 0
    for email in sorted(e for e in emails if e and "@" in e):
        try:
            r = requests.get(f"{iss}/kh/users/{email}/projects", headers={"Authorization": f"Bearer {bearer}"},
                             timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            projects = data.get("projects") if isinstance(data, dict) else data
            if isinstance(projects, dict):
                projects = list(projects)
            if isinstance(data, dict) and data.get("role") == "maintainer":
                projects = ["*"]
            with sqldb.connect() as c:
                sqldb.upsert(c, "kh_owner_grant", ["email"], {"email": email,
                                                              "projects_json": core.dumps(sorted(projects or [])),
                                                              "source": "refresh", "refreshed_ms": sqldb.now_ms(c)})
            _pcache.pop(("grant", email), None)
            n += 1
        except Exception as e:  # noqa: BLE001
            log.debug("owner grant refresh for %s failed: %s", email, e)
    return n


CRONS = [("cron.owner_grants", 600, refresh_owner_grants)]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def attach(app: FastAPI) -> None:
    tag = ["v1 principals"]

    @app.post("/v1/token", tags=tag, summary="Namespace client token (client_credentials)")
    async def post_token(request: Request):
        ctype = (request.headers.get("content-type") or "").lower()
        if "json" in ctype:
            body = await core.read_object(request)
        else:
            form = await request.form()
            body = {k: v for k, v in form.items()}
        if body.get("grant_type", "client_credentials") != "client_credentials":
            raise CapError("invalid", "grant_type must be client_credentials")
        return await core.run(client_token, str(body.get("client_id") or ""), str(body.get("client_secret") or ""))

    @app.get("/v1/me", tags=tag, summary="Who am I on /v1")
    async def get_me(request: Request):
        return await core.run(me, request)

    @app.post("/v1/admin/ns/{ns:ns}:client", tags=tag, summary="Provision or rotate the namespace client")
    async def post_client(ns: str, request: Request):
        def _do():
            with sqldb.connect() as c:
                r = c.execute("SELECT * FROM kh_ns WHERE ns=?", (ns,)).fetchone()
            row = core.decode_ns_row(r) if r else None
            if row is None or row["state"] not in ("active", "suspended"):
                raise CapError("ns_not_active", f"namespace '{ns}' is not active")
            core.admin_person([], row)
            _note_current()
            return provision_client(ns, row, request)

        return core.reply(await core.run(_do), status=201)

    @app.post("/v1/{ns}/principals", tags=tag, summary="Create a service principal")
    @core.idempotent
    async def post_principal(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        body = await core.read_object(request)
        out = await core.run(create, ctx, body)
        return core.reply(out, status=201, etag_rev=out["rev"])

    @app.get("/v1/{ns}/principals", tags=tag, summary="List service principals")
    async def get_principals(ns: str, role: str | None = Query(None), state: str | None = Query(None),
                             limit: str | None = Query(None), ctx: Ctx = Depends(core.ns_ctx)):
        _may_manage(ctx)
        lim = core.clamp_limit(limit, 100, LIST_MAX)
        return {"principals": await core.run(list_, ctx, role, state, lim)}

    @app.get("/v1/{ns}/principals/{pid}", tags=tag, summary="Read a service principal")
    async def get_principal(ns: str, pid: str, ctx: Ctx = Depends(core.ns_ctx)):
        if not (ctx.is_admin or ctx.allows("admin", "principals") or ctx.principal == pid):
            raise CapError("forbidden", "only a namespace admin or an orchestrator reads principals")
        out = await core.run(get, ctx, pid)
        return core.reply(out, etag_rev=out["rev"])

    @app.patch("/v1/{ns}/principals/{pid}", tags=tag, summary="Suspend, resume or revoke a principal")
    async def patch_principal(request: Request, ns: str, pid: str, ctx: Ctx = Depends(core.ns_ctx)):
        body = await core.read_object(request)
        out = await core.run(patch, ctx, pid, body, core.if_match(request))
        return core.reply(out, etag_rev=out["rev"])

    @app.post("/v1/{ns}/principals/{pid}/token", tags=tag, summary="Issue a token for a principal")
    @core.idempotent
    async def post_principal_token(request: Request, ns: str, pid: str, ctx: Ctx = Depends(core.ns_ctx)):
        return core.reply(await core.run(token_for, ctx, pid))
