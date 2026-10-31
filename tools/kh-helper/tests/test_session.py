import json
import os
import threading
import urllib.request

import pytest

from conftest import ISSUER, KH, jwt, make_session
from kh_helper import owner
from kh_helper import session as S


def test_home_and_defaults(env):
    assert S.home() == str(env["home"])
    assert S.DEFAULT_KH_URL == "http://localhost:8000"
    assert S.HTTP_TIMEOUT == 60
    from kh_helper import __version__
    assert __version__ == "1.19.3"


def test_device_login_stores_refresh_in_keyring(server, kr, env):
    calls = []

    def device(r):
        return 200, {"device_code": "dc", "user_code": "ABCD", "verification_uri_complete": "https://kc/x",
                     "interval": 1, "expires_in": 600}
    state = {"n": 0}

    def token(r):
        calls.append(r.form)
        if r.form["grant_type"].endswith("device_code"):
            state["n"] += 1
            if state["n"] == 1:
                return 400, {"error": "authorization_pending"}
            return 200, server.tokens("dev")
        return 400, {"error": "x"}
    server.on("POST", ISSUER + "/protocol/openid-connect/auth/device", device)
    server.on("POST", ISSUER + "/protocol/openid-connect/token", token)
    s = make_session()
    out = []
    s.print = out.append
    res = s.login(no_browser=True)
    assert res["email"] == "ann@example.com"
    assert any("Code: ABCD" in o for o in out)
    acct = f"{KH} {env['home']}"
    assert kr.store[("kh-helper", acct)]
    cfg = json.loads((env["home"] / "helper.json").read_text(encoding="utf-8"))
    assert cfg["kh_url"] == KH and cfg["issuer"] == ISSUER and cfg["client_id"] == "kh-cli"
    assert cfg["signed_in"]["email"] == "ann@example.com" and cfg["signed_in"]["until"].endswith("Z")
    assert cfg["owner"]["cli"] == "other" and cfg["owner"]["as"] == "claude-code"
    assert cfg["signed_in_to"] == [KH] and cfg["issuers"][KH] == ISSUER
    # no token on disk
    assert "refresh_token" not in json.dumps(cfg) and kr.store[("kh-helper", acct)] not in json.dumps(cfg)
    assert calls[-1]["code_verifier"]


def test_browser_login_pkce(server, env):
    seen = {}

    def token(r):
        seen.update(r.form)
        return 200, server.tokens("browser")
    server.on("POST", ISSUER + "/protocol/openid-connect/token", token)
    s = make_session()

    def open_browser(url):
        import urllib.parse as up
        q = dict(up.parse_qsl(up.urlsplit(url).query))
        assert q["code_challenge_method"] == "S256" and q["scope"] == "openid offline_access"
        cb = q["redirect_uri"] + "?code=abc&state=" + q["state"]

        def hit():
            urllib.request.urlopen(cb, timeout=10).read()
        threading.Thread(target=hit, daemon=True).start()
    res = s.login(open_browser=open_browser)
    assert res["email"] == "ann@example.com"
    assert seen["grant_type"] == "authorization_code" and seen["code"] == "abc" and seen["code_verifier"]


def test_refresh_and_rotation(signed_in, server, kr, env):
    s = make_session()
    st, body = s.call("GET", "/auth/me") if False else (None, None)
    server.kh("GET", "/auth/me", {"email": "ann@example.com"})
    st, body = s.call("GET", "/auth/me")
    assert st == 200 and body["email"] == "ann@example.com"
    assert server.refresh_count == 1
    stored = kr.store[("kh-helper", f"{KH} {env['home']}")]
    assert json.loads(__import__("base64").urlsafe_b64decode(stored.split(".")[1] + "==")).get("tag") == "rt-1"
    # cached access token: no second refresh
    s.call("GET", "/auth/me")
    assert server.refresh_count == 1


def test_revoked_refresh_raises_not_signed_in(signed_in, server, kr, env):
    kr.store[("kh-helper", f"{KH} {env['home']}")] = "bad-token"
    s = make_session()
    with pytest.raises(S.NotSignedIn) as e:
        s.call("GET", "/auth/me")
    assert "kh login" in str(e.value)


def test_not_signed_in(server):
    with pytest.raises(S.NotSignedIn):
        make_session().call("GET", "/auth/me")


def test_headers_actor_and_acting(signed_in, server, monkeypatch, env):
    server.kh("GET", "/x", {"ok": True})
    monkeypatch.setenv("KH_ACTOR", "sess one/../")
    monkeypatch.setenv("KH_ACTING_TOKEN", "acting-123")
    s = make_session()
    s.call("GET", "/x")
    h = server.find("GET", "/x")[-1].headers
    assert h["Authorization"].startswith("Bearer ey")
    assert h["X-KH-Actor"] == "sessone.."
    assert h["X-KH-Acting-Token"] == "acting-123"
    # explicit actor wins; "" = none
    s.actor = ""
    s.call("GET", "/x")
    assert "X-KH-Actor" not in server.find("GET", "/x")[-1].headers


def test_actor_from_session_actors_file(signed_in, server, monkeypatch, env):
    monkeypatch.setenv("CLAUDE_PID", "4242")
    S.remember_actor("abc-123")
    server.kh("GET", "/x", {"ok": True})
    make_session().call("GET", "/x")
    assert server.find("GET", "/x")[-1].headers["X-KH-Actor"] == "abc-123"


def test_acting_token_file(signed_in, server, monkeypatch, env):
    f = env["tmp"] / "acting.txt"
    f.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("KH_ACTING_TOKEN_FILE", str(f))
    server.kh("GET", "/x", {})
    make_session().call("GET", "/x")
    assert server.find("GET", "/x")[-1].headers["X-KH-Acting-Token"] == "from-file"


def test_401_retry_once_and_token_refresh_needed(signed_in, server):
    n = {"c": 0}

    def h(r):
        n["c"] += 1
        return (401, {"detail": "expired"}) if n["c"] == 1 else (200, {"token_refresh_needed": True})
    server.kh("GET", "/y", h)
    s = make_session()
    st, body = s.call("GET", "/y")
    assert st == 200 and n["c"] == 2
    assert s._access is None  # dropped because token_refresh_needed


def test_unmangle_and_actor_id():
    assert S._unmangle("C:/Program Files/Git/vector/add") == "/vector/add"
    assert S._unmangle("vector/add") == "/vector/add"
    assert S.actor_id("_x" + "a" * 200) == "x" + "a" * 127
    assert S.actor_id("") == ""


def test_call_identity(signed_in, server):
    server.on("GET", ISSUER + "/kh/catalog", lambda r: (200, {"levels": ["owner"]}))
    st, body = make_session().call_identity("GET", "catalog")
    assert st == 200 and body["levels"] == ["owner"]
    assert server.requests[-1].headers["Authorization"].startswith("Bearer ")


def test_logout_and_status(signed_in, server, kr, env):
    s = make_session()
    st = s.status()
    assert st["signed_in"] is True and st["email"] == "ann@example.com" and "token" not in json.dumps(st).lower()
    assert s.logout() == "revoked"
    assert server.revoked and server.revoked[0]["token_type_hint"] == "refresh_token"
    assert make_session().status()["signed_in"] is False
    assert s.logout() == "none"


def test_owner_lock_claude_owned_refuses_other_cli(server, env, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    s = make_session()
    s._store(server.tokens())
    cfg = S.load_config()
    assert cfg["owner"] == {"cli": "claude-code", "profile": str(env["profile"])}
    # Claude Code with the same profile: ok
    s2 = make_session()
    s2.status()
    # another CLI (no CLAUDECODE): refused
    monkeypatch.delenv("CLAUDECODE")
    with pytest.raises(owner.WrongOwner):
        make_session().call("GET", "/auth/me")


def test_owner_rules_pure():
    o = {"cli": "claude-code", "profile": "/p"}
    assert owner.allowed(o, {"cli": "claude-code", "profile": "/p"})
    assert not owner.allowed(o, {"cli": "claude-code", "profile": "/q"})
    assert not owner.allowed(o, {"cli": "other"})
    assert not owner.allowed({"cli": "other", "profile": "/g", "as": "grok"}, {"cli": "claude-code", "profile": "/g"})
    assert owner.allowed({"cli": "other", "profile": "/g", "as": "grok"}, {"cli": "other"})


def test_jwt_claims():
    assert S._claims(jwt({"email": "a@b"}))["email"] == "a@b"
    assert S._claims("garbage") == {}
