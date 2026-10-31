"""Signed-in test helpers (owned by §2; reusable by §3/§4 tests).

    from tests.auth_helpers import enable_signin, make_token, bearer

    def test_x(monkeypatch, client):          # or use the `signin` / `signed_client` fixtures of tests/auth
        enable_signin(monkeypatch)
        h = bearer(make_token("a@x.com", projects={"p1": "editable"}))
        client.get("/projects", headers=h)

make_token(email, role="member", projects={}, all_projects=[], permissions=[], **claims)
    projects: {pid: [perms]} or {pid: "owner"|"editable"|"readonly"} (level names expand to their permissions)
    claims: extra / overriding top-level claims (exp, iat, iss, aud, azp, email_verified, kh=None to drop the
            kh claim). Special keys: _kid (header kid), _key (private key, e.g. OTHER_KEY), _iss_jwks (unused).
"""
from __future__ import annotations

import time

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://kc.test/realms/knowledge-hub"
AUDIENCE = "kh-api"
KID = "kh-test-key"

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

LEVELS = {
    "owner": ["project.read", "project.write", "project.log", "project.members.manage", "project.delete"],
    "editable": ["project.read", "project.write", "project.log"],
    "readonly": ["project.read"],
}
MAINTAINER_PROJECT_PERMS = list(LEVELS["owner"])
MAINTAINER_KH_PERMS = ["project.create", "kh.users.manage", "kh.users.invite", "kh.maintainers.manage"]


def jwk_of(private_key, kid: str = KID) -> dict:
    import json

    d = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    d.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return d


def jwks(*extra: dict) -> dict:
    return {"keys": [jwk_of(PRIVATE_KEY), *extra]}


def make_token(email: str, role: str = "member", projects: dict | None = None, all_projects: list | None = None,
               permissions: list | None = None, **claims) -> str:
    kid = claims.pop("_kid", KID)
    key = claims.pop("_key", PRIVATE_KEY)
    claims.pop("_iss_jwks", None)
    perms_by_project = {}
    for pid, v in (projects or {}).items():
        perms_by_project[pid] = list(LEVELS[v]) if isinstance(v, str) else list(v)
    all_p = list(all_projects or [])
    kh_perms = list(permissions or [])
    if role == "maintainer" and all_projects is None:
        all_p = list(MAINTAINER_PROJECT_PERMS)
    if role == "maintainer" and permissions is None:
        kh_perms = list(MAINTAINER_KH_PERMS)
    now = int(time.time())
    body = {"iss": ISSUER, "aud": AUDIENCE, "sub": email, "iat": now, "exp": now + 300, "email": email,
            "email_verified": True, "azp": "kh-cli",
            "kh": {"email": email, "role": role, "permissions": kh_perms, "all_projects": all_p,
                   "projects": perms_by_project}}
    for k, v in claims.items():
        if v is None:
            body.pop(k, None)
        else:
            body[k] = v
    return jwt.encode(body, key, algorithm="RS256", headers={"kid": kid})


def bearer(token: str, actor: str | None = None, **extra) -> dict:
    h = {"Authorization": f"Bearer {token}"}
    if actor:
        h["X-KH-Actor"] = actor
    h.update(extra)
    return h


def enable_signin(monkeypatch, issuer: str = ISSUER, jwks_doc: dict | None = None, fetch=None) -> dict:
    """Switch sign-in on for one test: KH_AUTH_ISSUER + JWKS fetch served from the local RSA key.

    Returns a dict {"calls": [urls]} counting JWKS fetches.
    """
    from app import auth, config

    state = {"calls": [], "docs": {}}
    doc = jwks_doc or jwks()
    monkeypatch.setenv("KH_AUTH_ISSUER", issuer)
    monkeypatch.setattr(config, "AUTH_ISSUER", issuer.rstrip("/"))

    def _fetch(url):
        state["calls"].append(url)
        if fetch is not None:
            return fetch(url)
        if url in state["docs"]:
            return state["docs"][url]
        return doc

    monkeypatch.setattr(auth, "_fetch_jwks", _fetch)
    auth.reset_caches()
    monkeypatch.setattr(auth, "relay", lambda method, path, body=None, timeout=10.0: (200, {}))
    return state
