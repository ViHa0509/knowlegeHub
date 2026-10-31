"""Shared fixtures: temp profile/HOME, memory keyring, fake KH + Keycloak behind a requests adapter.

Nothing here touches the real ~/.claude, a real keyring, a real KH or a real Keycloak.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.parse

import keyring
import keyring.backend
import pytest
import requests
from requests.adapters import BaseAdapter
from requests.models import Response

KH = "https://kh.test"
ISSUER = "https://kc.test/realms/knowledge-hub"


def jwt(claims: dict) -> str:
    def enc(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'none'})}.{enc(claims)}.sig"


class MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.store: dict = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


class FakeServer:
    """Routes: (METHOD, url-without-query) -> handler(req) -> (status, body[, headers])."""

    def __init__(self):
        self.routes: dict = {}
        self.requests: list = []
        self.refresh_count = 0
        self.email = "ann@example.com"
        self.revoked: list = []
        self.down = False
        self.default_routes()

    def default_routes(self):
        self.on("GET", KH + "/auth/config", lambda r: (200, {"issuer": ISSUER, "client_id": "kh-cli",
                                                              "audience": "kh", "help": "/auth/help"}))
        self.on("POST", ISSUER + "/protocol/openid-connect/token", self._token)
        self.on("POST", ISSUER + "/protocol/openid-connect/revoke",
                lambda r: (self.revoked.append(r.form) or 200, ""))

    def _token(self, r):
        f = r.form
        if f.get("grant_type") == "refresh_token":
            if f.get("refresh_token", "").startswith("bad"):
                return 400, {"error": "invalid_grant"}
            self.refresh_count += 1
        return 200, self.tokens(f"rt-{self.refresh_count}")

    def tokens(self, refresh_tag: str = "rt-0") -> dict:
        now = int(time.time())
        return {"access_token": jwt({"email": self.email, "exp": now + 300, "n": self.refresh_count}),
                "refresh_token": jwt({"exp": now + 90 * 86400, "tag": refresh_tag}), "expires_in": 300}

    def on(self, method: str, url: str, handler):
        self.routes[(method.upper(), url)] = handler

    def kh(self, method: str, path: str, handler_or_body, status: int = 200):
        h = handler_or_body if callable(handler_or_body) else (lambda r, b=handler_or_body: (status, b))
        self.on(method, KH + path, h)

    def find(self, method: str, path: str) -> list:
        return [r for r in self.requests if r.method == method and r.path == path]


class FakeRequest:
    def __init__(self, prep):
        self.method = prep.method
        u = urllib.parse.urlsplit(prep.url)
        self.url = prep.url
        self.base = f"{u.scheme}://{u.netloc}{u.path}"
        self.path = u.path
        self.params = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        self.headers = dict(prep.headers)
        body = prep.body
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        self.raw = body or ""
        ctype = self.headers.get("Content-Type", "")
        self.json = json.loads(self.raw) if "json" in ctype and self.raw else None
        self.form = {k: v[0] for k, v in urllib.parse.parse_qs(self.raw).items()} if "form" in ctype else {}


class FakeAdapter(BaseAdapter):
    def __init__(self, server: FakeServer):
        super().__init__()
        self.server = server

    def send(self, request, **kw):
        if self.server.down:
            raise requests.exceptions.ConnectionError("down (NewConnectionError)")
        req = FakeRequest(request)
        self.server.requests.append(req)
        handler = self.server.routes.get((req.method, req.base))
        if handler is None:
            out = (404, {"detail": "Not Found"})
        else:
            out = handler(req)
        status, body = out[0], out[1]
        headers = out[2] if len(out) > 2 else {}
        resp = Response()
        resp.status_code = status
        resp.request = request
        resp.url = request.url
        if isinstance(body, (dict, list)):
            resp._content = json.dumps(body).encode()
            resp.headers["content-type"] = "application/json"
        else:
            resp._content = str(body).encode()
            resp.headers["content-type"] = "text/plain; charset=utf-8"
        resp.headers.update(headers)
        resp.encoding = "utf-8"
        return resp

    def close(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Temp user home, Claude profile and helper HOME; Claude Code caller by default off."""
    user = tmp_path / "user"
    profile = user / ".claude-test"
    home = profile / "kh-helper"
    home.mkdir(parents=True)
    for k in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(k, str(user))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    monkeypatch.setenv("KH_HELPER_HOME", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("GROK_HOME", str(user / ".grok"))
    monkeypatch.setenv("CODEX_HOME", str(user / ".codex"))
    monkeypatch.setenv("KH_HELPER_OUTCOMES", "off")
    monkeypatch.setenv("KH_HELPER_WORKLOG", "off")
    for k in ("CLAUDECODE", "CLAUDE_PID", "KH_ACTOR", "CLAUDE_CODE_SESSION_ID", "KH_ACTING_TOKEN",
              "KH_ACTING_TOKEN_FILE", "KH_URL", "KH_HELPER_SUMMARIZER", "KH_HELPER_WORKLOG_CMD",
              "KH_HELPER_HOOK_SECONDS", "KH_HELPER_STARTUP_SECONDS"):
        monkeypatch.delenv(k, raising=False)
    from kh_helper import hooks, worklog
    real_which = __import__("shutil").which
    fake_which = (lambda name, *a, **k: None if name == "claude" else real_which(name, *a, **k))
    monkeypatch.setattr(worklog.shutil, "which", fake_which)
    monkeypatch.setattr(hooks, "hook_check", lambda *a, **k: {"git-bash": "ok"})
    return {"user": user, "profile": profile, "home": home, "tmp": tmp_path}


@pytest.fixture
def kr():
    old = keyring.get_keyring()
    mem = MemoryKeyring()
    keyring.set_keyring(mem)
    yield mem
    keyring.set_keyring(old)


@pytest.fixture
def server(env, kr, monkeypatch):
    srv = FakeServer()
    from kh_helper import session as S

    def factory():
        s = requests.Session()
        s.mount("https://", FakeAdapter(srv))
        s.mount("http://kh", FakeAdapter(srv))
        return s

    monkeypatch.setattr(S, "HTTP_FACTORY", factory)
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    return srv


@pytest.fixture
def claude_code(env, monkeypatch):
    """Pretend Claude Code (same profile) runs us."""
    import os
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    return env


@pytest.fixture
def signed_in(server, env):
    """A stored sign-in (as a terminal login would leave it) for KH."""
    from kh_helper import session as S
    sess = S.Session(KH)
    sess.print = lambda *a, **k: None
    sess._store(server.tokens())
    return sess


def make_session(kh_url: str = KH):
    from kh_helper import session as S
    s = S.Session(kh_url)
    s.print = lambda *a, **k: None
    return s
