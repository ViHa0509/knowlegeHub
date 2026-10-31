"""§2.2-2.3 / §2.15: modes, token validation, 401/403/503 texts."""
from __future__ import annotations

import time

import jwt
import pytest

from app import auth
from tests.auth_helpers import ISSUER, OTHER_KEY, PRIVATE_KEY, bearer, enable_signin, make_token

SIGN_IN = {"detail": "sign-in required", "help": "/auth/help"}


def test_open_mode_has_no_gate(client):
    assert auth.enabled() is False
    assert client.get("/projects").status_code == 200
    assert client.get("/auth/config").json()["issuer"] is None


def test_public_routes_need_no_token(signed_client):
    c = signed_client
    assert c.get("/health").status_code == 200
    assert c.get("/version").status_code == 200
    cfg = c.get("/auth/config").json()
    assert cfg["issuer"] == ISSUER and cfg["client_id"] == "kh-cli" and cfg["audience"] == "kh-api"
    assert cfg["help"] == "/auth/help" and "helper" in cfg and "helper_version" in cfg
    r = c.get("/auth/help")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    assert ISSUER in r.text and "{issuer}" not in r.text and "{kh_url}" not in r.text


def test_missing_bearer_is_401(signed_client):
    r = signed_client.get("/projects")
    assert r.status_code == 401 and r.json() == SIGN_IN
    r = signed_client.post("/vector/add", json={"text": "x"})
    assert r.status_code == 401 and r.json() == SIGN_IN


def test_options_passes_without_token(signed_client):
    r = signed_client.options("/projects")
    assert r.status_code != 401


@pytest.mark.parametrize("token,detail", [
    ("not-a-jwt", "malformed token"),
    (lambda: make_token("a@x.com", _kid="nope"), "unknown signing key"),
    (lambda: make_token("a@x.com", exp=int(time.time()) - 120, iat=int(time.time()) - 600), "token expired"),
    (lambda: make_token("a@x.com", _key=OTHER_KEY), "invalid token: "),
    (lambda: make_token("a@x.com", aud="other"), "invalid token: "),
    (lambda: make_token("a@x.com", iss="https://evil.test/realms/x"), "invalid token: "),
])
def test_bad_tokens_are_401(signed_client, token, detail):
    tok = token() if callable(token) else token
    r = signed_client.get("/projects", headers=bearer(tok))
    assert r.status_code == 401
    body = r.json()
    assert body["help"] == "/auth/help"
    assert body["detail"] == detail if not detail.endswith(": ") else body["detail"].startswith(detail)


def test_expiry_leeway_30s(signed_client):
    now = int(time.time())
    ok = make_token("leeway@x.com", exp=now - 10, iat=now - 300)
    assert signed_client.get("/projects", headers=bearer(ok)).status_code == 200


def test_jwks_unreachable_is_503(monkeypatch, client):
    def boom(url):
        raise ConnectionError("down")

    enable_signin(monkeypatch, fetch=boom)
    r = client.get("/projects", headers=bearer(make_token("a@x.com")))
    assert r.status_code == 503 and r.json() == {"detail": "sign-in service unavailable"}


def test_jwks_cached_and_refreshed_on_unknown_kid(signin, signed_client):
    tok = make_token("cache@x.com")
    signed_client.get("/projects", headers=bearer(tok))
    signed_client.get("/projects", headers=bearer(tok))
    assert len(signin["calls"]) == 1
    assert signin["calls"][0] == ISSUER + "/protocol/openid-connect/certs"
    signed_client.get("/projects", headers=bearer(make_token("cache@x.com", _kid="rotated")))
    assert len(signin["calls"]) == 2


def test_not_registered_is_403(signed_client):
    for tok in (make_token("a@x.com", kh=None), make_token("a@x.com", role="guest"), make_token("", kh=None)):
        r = signed_client.get("/projects", headers=bearer(tok))
        assert r.status_code == 403 and r.json() == {"detail": "not a registered KH user"}
    tok = jwt.encode({"iss": ISSUER, "aud": "kh-api", "iat": int(time.time()), "exp": int(time.time()) + 60,
                      "kh": {"email": "", "role": "member"}}, PRIVATE_KEY,
                     algorithm="RS256", headers={"kid": "kh-test-key"})
    assert signed_client.get("/projects", headers=bearer(tok)).json() == {"detail": "not a registered KH user"}


def test_unlisted_route_is_maintainer_only(signed_client):
    r = signed_client.get("/openapi.json", headers=bearer(make_token("m@x.com")))
    assert r.status_code == 403 and r.json() == {"detail": "route not open to members"}
    r = signed_client.get("/openapi.json", headers=bearer(make_token("boss@x.com", role="maintainer")))
    assert r.status_code == 200


def test_maintainer_kind(signed_client):
    r = signed_client.post("/projects/registry/sync", headers=bearer(make_token("m@x.com")))
    assert r.status_code == 403 and r.json() == {"detail": "maintainers only"}
    r = signed_client.post("/projects/registry/sync", headers=bearer(make_token("boss@x.com", role="maintainer")))
    assert r.status_code == 200 and set(r.json()) == {"written", "unchanged", "total"}


def test_namespace_client_token_only_v1(signed_client):
    tok = make_token("ignored@x.com", kh=None, azp="kh-ns-alpha")
    r = signed_client.get("/projects", headers=bearer(tok))
    assert r.status_code == 403 and r.json() == {"detail": "service tokens may call /v1 routes only"}
    p = auth.Principal.from_claims({"azp": "kh-ns-alpha"})
    assert p.ns == "alpha" and p.email == "client:alpha" and p.svc_role == "admin" and p.principal_id == "client"


def test_kh_service_token_without_caps_is_401(signed_client):
    try:
        import app.caps.principals  # noqa: F401
        pytest.skip("§6 principals present")
    except ImportError:
        pass
    tok = jwt.encode({"iss": "kh-principals", "aud": "kh-v1", "sub": "ns:p"}, "secret", algorithm="HS256")
    r = signed_client.get("/v1/me", headers=bearer(tok))
    assert r.status_code == 401 and r.json()["detail"].startswith("invalid token")


def test_dev_bootstrap(monkeypatch, signed_client):
    token = "d" * 40
    monkeypatch.setenv("KH_DEV_BOOTSTRAP", "1")
    monkeypatch.setenv("KH_DEV_BOOTSTRAP_TOKEN", token)
    r = signed_client.get("/auth/me", headers=bearer("khdev." + token))
    assert r.status_code == 200
    assert r.json()["email"] == "dev-admin@kh.local" and r.json()["role"] == "maintainer"
    assert signed_client.get("/auth/me", headers=bearer("khdev." + "x" * 40)).status_code == 401
    monkeypatch.setenv("KH_DEV_BOOTSTRAP_TOKEN", "short")
    assert signed_client.get("/auth/me", headers=bearer("khdev.short")).status_code == 401
    monkeypatch.setenv("KH_DEV_BOOTSTRAP_TOKEN", token)
    monkeypatch.setenv("CF_INSTANCE_GUID", "abc")
    assert signed_client.get("/auth/me", headers=bearer("khdev." + token)).status_code == 401


def test_auth_me(signed_client):
    tok = make_token("Jane.Doe@Example.com", projects={"alpha": "readonly"}, permissions=["project.create"])
    r = signed_client.get("/auth/me", headers=bearer(tok))
    assert r.status_code == 200
    me = r.json()
    assert me == {"email": "jane.doe@example.com", "personal_project": "personal-jane-doe-example-com",
                  "role": "member", "permissions": ["project.create"], "all_projects": [],
                  "projects": {"alpha": ["project.read"]}}
    from app import projects

    assert projects.exists("personal-jane-doe-example-com")
    reg = signed_client.get("/projects/registry", headers=bearer(tok)).json()
    assert "personal-jane-doe-example-com" in [p["id"] for p in reg["projects"]]


def test_auth_help_put_and_get(signed_client):
    m = bearer(make_token("boss@x.com", role="maintainer"))
    assert signed_client.put("/auth/help", json={}, headers=m).status_code == 422
    assert signed_client.put("/auth/help", json={"text": "x"}, headers=bearer(make_token("u@x.com"))).status_code == 403
    r = signed_client.put("/auth/help", json={"text": "Guide for {kh_url} via {issuer}: {source}"}, headers=m)
    assert r.json() == {"doc": "auth-help", "id": "kh-guide-auth-help", "chars": len("Guide for {kh_url} via {issuer}: {source}")}
    text = signed_client.get("/auth/help", headers={"x-forwarded-proto": "https", "x-forwarded-host": "kh.example"}).text
    assert text.startswith(f"Guide for https://kh.example via {ISSUER}: https://kh.example/auth/download/")


def test_auth_download_404_and_bundled(signed_client, tmp_path, monkeypatch):
    from app import auth_routes

    assert signed_client.get("/auth/download/nothing.whl").status_code == 404
    monkeypatch.setattr(auth_routes, "DOWNLOADS_DIR", tmp_path)
    (tmp_path / "kh_helper-1.19.3-py3-none-any.whl").write_bytes(b"PK\x03\x04wheel")
    (tmp_path / "kh_helper-1.9.0-py3-none-any.whl").write_bytes(b"PK\x03\x04old")
    r = signed_client.get("/auth/download/kh-helper")
    assert r.status_code == 200 and r.content == b"PK\x03\x04wheel"
    cfg = signed_client.get("/auth/config").json()
    assert cfg["helper"].endswith("/auth/download/kh_helper-1.19.3-py3-none-any.whl")
    assert cfg["helper_version"] == "1.19.3"


def test_api_call_event_emitted(monkeypatch, signed_client):
    from app.eventing import emit as em

    seen = []
    monkeypatch.setattr(em, "emit", lambda kind, body=None, **kw: seen.append((kind, body, kw)))
    signed_client.get("/projects?hook_type=UserPromptSubmit", headers=bearer(make_token("ev@x.com")))
    calls = [s for s in seen if s[0] == "kh.api.call"]
    assert calls and calls[-1][1]["path"] == "/projects" and calls[-1][1]["status"] == 200
    assert calls[-1][1]["hook_type"] == "UserPromptSubmit" and calls[-1][2]["email"] == "ev@x.com"
    seen.clear()
    signed_client.get("/health")
    assert not [s for s in seen if s[0] == "kh.api.call"]
