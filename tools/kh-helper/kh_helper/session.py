"""Sign-in, token lifecycle and every KH call.

The helper is the only holder of credentials:
- the refresh token lives in the OS keyring (service 'kh-helper', account '<kh_url> <HOME>');
- access tokens (Keycloak, ~5 min) live only in process memory;
- helper.json under HOME holds non-secret state (kh_url, issuer, client_id, signed_in, owner, ...).
The assistant never sees a password, refresh token or access token; it gets JSON answers.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from . import clis, owner

KEYRING_SERVICE = "kh-helper"
DEFAULT_KH_URL = "http://localhost:8000"
HTTP_TIMEOUT = 60
DEFAULT_CLIENT_ID = "kh-cli"
CONFIG_NAME = "helper.json"
ACTORS_NAME = "session-actors.json"
ACTOR_DAYS = 7
LOGIN_WAIT_SECONDS = 600
SCOPE = "openid offline_access"
LEGACY_HOME = os.path.join("~", ".kh")
ACTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

# Test seam: the requests.Session factory used for every HTTP call.
HTTP_FACTORY = requests.Session


class NotSignedIn(Exception):
    """Nobody is signed in on this machine, or the sign-in ended."""


class PasswordNotChanged(Exception):
    """kh password: Keycloak did not report kc_action_status=success."""


class LoginFailed(Exception):
    pass


# ---------------------------------------------------------------- paths

def cli_profile() -> str:
    return clis.profile(clis.CLAUDE)


def home() -> str:
    """Where this copy keeps state."""
    env = os.environ.get("KH_HELPER_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if sys.prefix != sys.base_prefix:
        return os.path.abspath(sys.prefix)
    return os.path.join(cli_profile(), "kh-helper")


def is_legacy_home(h: str) -> bool:
    return os.path.normcase(os.path.abspath(h)) == os.path.normcase(os.path.abspath(os.path.expanduser(LEGACY_HOME)))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path: str, data) -> None:
    """Atomic UTF-8 JSON write (tmp then replace)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def config_path() -> str:
    return os.path.join(home(), CONFIG_NAME)


def load_config() -> dict:
    data = read_json(config_path(), {})
    return data if isinstance(data, dict) else {}


def save_config(cfg: dict) -> None:
    write_json(config_path(), cfg)


# ---------------------------------------------------------------- keyring

def _keyring():
    import keyring
    return keyring


def keyring_account(kh_url: str, h: str | None = None) -> str:
    h = h or home()
    return kh_url if is_legacy_home(h) else f"{kh_url} {h}"


def get_refresh(kh_url: str) -> str | None:
    try:
        return _keyring().get_password(KEYRING_SERVICE, keyring_account(kh_url))
    except Exception:  # noqa: BLE001
        return None


def set_refresh(kh_url: str, token: str) -> None:
    _keyring().set_password(KEYRING_SERVICE, keyring_account(kh_url), token)


def delete_refresh(kh_url: str) -> None:
    try:
        _keyring().delete_password(KEYRING_SERVICE, keyring_account(kh_url))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- helpers

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def pkce_pair(n: int = 64) -> tuple[str, str]:
    verifier = secrets.token_urlsafe(n)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _claims(token: str | None) -> dict:
    """Decode a JWT payload without verification."""
    if not token or token.count(".") < 2:
        return {}
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
    except Exception:  # noqa: BLE001
        return {}


def _unmangle(path: str) -> str:
    """Undo Git-Bash MSYS path conversion ('C:/Program Files/Git/vector/add' -> '/vector/add')."""
    p = (path or "").strip()
    m = re.match(r"^[A-Za-z]:[/\\].*?[/\\]Git((?:[/\\].*)?)$", p)
    if m:
        p = m.group(1).replace("\\", "/") or "/"
    if not p.startswith("/"):
        p = "/" + p
    return p


def actor_id(value) -> str:
    """Sanitise to [A-Za-z0-9_.:-], max 128, must start alnum; '' if nothing usable."""
    s = re.sub(r"[^A-Za-z0-9_.:-]", "", str(value or ""))
    s = s.lstrip("_.:-")[:128]
    return s if ACTOR_RE.match(s) else ""


def actors_path() -> str:
    return os.path.join(home(), ACTORS_NAME)


def remember_actor(actor: str) -> None:
    """Map CLAUDE_PID -> actor, so the MCP server (which keeps its startup env) follows /clear and /resume."""
    pid = os.environ.get("CLAUDE_PID")
    if not pid or not actor:
        return
    data = read_json(actors_path(), {}) or {}
    cutoff = time.time() - ACTOR_DAYS * 86400
    data = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("at", 0) >= cutoff}
    data[pid] = {"actor": actor, "at": time.time()}
    write_json(actors_path(), data)


def actor_from_env(explicit: str | None = None) -> str:
    if explicit is not None:
        return actor_id(explicit)
    env = os.environ.get("KH_ACTOR")
    if env:
        return actor_id(env)
    pid = os.environ.get("CLAUDE_PID")
    if pid:
        entry = (read_json(actors_path(), {}) or {}).get(pid)
        if isinstance(entry, dict) and entry.get("actor"):
            return actor_id(entry["actor"])
    return actor_id(os.environ.get("CLAUDE_CODE_SESSION_ID", ""))


def acting_from_env() -> str | None:
    tok = os.environ.get("KH_ACTING_TOKEN")
    if tok:
        return tok.strip()
    path = os.environ.get("KH_ACTING_TOKEN_FILE")
    if path:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            return None
    return None


# ---------------------------------------------------------------- loopback callback

class _Callback(BaseHTTPRequestHandler):
    result: dict | None = None

    def do_GET(self):  # noqa: N802
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" not in q and "error" not in q:
            self.send_response(404)
            self.end_headers()
            return
        self.server.result = {k: v[0] for k, v in q.items()}  # type: ignore[attr-defined]
        ok = "code" in q
        msg = "You're signed in to Knowledge Hub." if ok else "Sign-in did not complete."
        body = (f"<html><body><p>{html.escape(msg)}</p><p>You can close this window.</p></body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


# ---------------------------------------------------------------- Session

class Session:
    def __init__(self, kh_url: str | None = None, actor: str | None = None):
        self.cfg = load_config()
        self.kh_url = (kh_url or self.cfg.get("kh_url") or os.environ.get("KH_URL") or DEFAULT_KH_URL).rstrip("/")
        self.http = HTTP_FACTORY()
        self.actor = actor  # None = resolve from env; "" = none
        self.acting_token: str | None = None
        self._access: str | None = None
        self._access_until = 0.0
        self._discovered: dict | None = None
        self.print = print

    # -- discovery
    def discover(self) -> dict:
        if self._discovered is None:
            r = self.http.get(f"{self.kh_url}/auth/config", timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            self._discovered = r.json() or {}
        return self._discovered

    def issuer(self) -> str:
        iss = (self.cfg.get("issuers") or {}).get(self.kh_url)
        if not iss and self.cfg.get("kh_url", "").rstrip("/") == self.kh_url:
            iss = self.cfg.get("issuer")
        if not iss:
            iss = self.discover().get("issuer")
        if not iss:
            raise LoginFailed(f"{self.kh_url} has no sign-in (no issuer in /auth/config)")
        return iss.rstrip("/")

    def client_id(self) -> str:
        cid = self.cfg.get("client_id") if self.cfg.get("kh_url", "").rstrip("/") == self.kh_url else None
        if not cid:
            try:
                cid = self.discover().get("client_id")
            except Exception:  # noqa: BLE001
                cid = None
        return cid or DEFAULT_CLIENT_ID

    def token_url(self) -> str:
        return f"{self.issuer()}/protocol/openid-connect/token"

    # -- owner
    def check_owner(self) -> None:
        owner.check(home(), self.cfg)

    # -- login
    def login(self, no_browser: bool = False, open_browser=None) -> dict:
        self.cfg = load_config()
        owner.login_check(home())
        self._discovered = None
        self.discover()
        if no_browser:
            tokens = self._device_flow()
        else:
            try:
                tokens = self._browser_flow(open_browser=open_browser)
            except OSError:
                tokens = self._device_flow()
        return self._store(tokens)

    def change_password(self, open_browser=None) -> dict:
        self.check_owner()
        tokens = self._browser_flow(kc_action="UPDATE_PASSWORD", open_browser=open_browser)
        return self._store(tokens)

    def _browser_flow(self, kc_action: str | None = None, open_browser=None) -> dict:
        verifier, challenge = pkce_pair(64)
        state = secrets.token_urlsafe(24)
        server = HTTPServer(("127.0.0.1", 0), _Callback)
        server.result = None  # type: ignore[attr-defined]
        server.timeout = 1
        port = server.server_address[1]
        redirect = f"http://127.0.0.1:{port}/callback"
        params = {
            "client_id": self.client_id(), "response_type": "code", "redirect_uri": redirect, "scope": SCOPE,
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        }
        if kc_action:
            params["kc_action"] = kc_action
        url = f"{self.issuer()}/protocol/openid-connect/auth?" + urllib.parse.urlencode(params)
        self.print(f"Sign in to Knowledge Hub in your browser:\n  {url}\n(waiting up to 10 minutes)")
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            (open_browser or webbrowser.open)(url)
        except Exception:  # noqa: BLE001
            pass
        deadline = time.monotonic() + LOGIN_WAIT_SECONDS
        try:
            while server.result is None and time.monotonic() < deadline:  # type: ignore[attr-defined]
                server.handle_request()
        finally:
            server.server_close()
        res = server.result  # type: ignore[attr-defined]
        if not res:
            raise LoginFailed("sign-in timed out")
        if res.get("state") != state:
            raise LoginFailed("sign-in answer had the wrong state")
        if "code" not in res:
            if kc_action:
                raise PasswordNotChanged(res.get("error_description") or res.get("error") or "not changed")
            raise LoginFailed(res.get("error_description") or res.get("error") or "sign-in did not complete")
        if kc_action and res.get("kc_action_status") != "success":
            raise PasswordNotChanged(res.get("kc_action_status") or "password not changed")
        r = self.http.post(self.token_url(), data={
            "grant_type": "authorization_code", "code": res["code"], "redirect_uri": redirect,
            "client_id": self.client_id(), "code_verifier": verifier,
        }, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            raise LoginFailed(f"token exchange failed ({r.status_code}): {r.text[:300]}")
        return r.json()

    def _device_flow(self, sleep=time.sleep) -> dict:
        verifier, challenge = pkce_pair(48)
        r = self.http.post(f"{self.issuer()}/protocol/openid-connect/auth/device", data={
            "client_id": self.client_id(), "scope": SCOPE,
            "code_challenge": challenge, "code_challenge_method": "S256",
        }, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            raise LoginFailed(f"device sign-in failed ({r.status_code}): {r.text[:300]}")
        d = r.json()
        interval = int(d.get("interval") or 5)
        expires = int(d.get("expires_in") or 600)
        self.print(f"Sign in to Knowledge Hub in your browser:\n  {d.get('verification_uri_complete') or d.get('verification_uri')}")
        self.print(f"Code: {d.get('user_code')} (valid {max(1, expires // 60)} minutes)")
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        deadline = time.monotonic() + expires
        while time.monotonic() < deadline:
            sleep(interval)
            t = self.http.post(self.token_url(), data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": d.get("device_code"), "client_id": self.client_id(), "code_verifier": verifier,
            }, timeout=HTTP_TIMEOUT)
            if t.status_code == 200:
                return t.json()
            err = (t.json() if t.headers.get("content-type", "").startswith("application/json") else {}).get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            raise LoginFailed(f"device sign-in failed: {err or t.status_code}")
        raise LoginFailed("device sign-in timed out")

    def _store(self, tokens: dict) -> dict:
        refresh = tokens.get("refresh_token")
        access = tokens.get("access_token")
        if not refresh or not access:
            raise LoginFailed("Keycloak returned no tokens")
        set_refresh(self.kh_url, refresh)
        email = _claims(access).get("email") or _claims(tokens.get("id_token")).get("email")
        exp = _claims(refresh).get("exp")
        until = datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if exp else None
        cfg = load_config()
        cfg["kh_url"] = self.kh_url
        cfg["issuer"] = self.issuer()
        cfg["client_id"] = self.client_id()
        cfg["signed_in"] = {"email": email, "until": until}
        cfg["owner"] = owner.record_for(home())
        urls = [u for u in (cfg.get("signed_in_to") or []) if u != self.kh_url]
        cfg["signed_in_to"] = urls + [self.kh_url]
        cfg.setdefault("issuers", {})[self.kh_url] = self.issuer()
        save_config(cfg)
        self.cfg = cfg
        self._access = access
        self._access_until = time.time() + int(tokens.get("expires_in") or 300) - 20
        return {"email": email, "until": until, "kh_url": self.kh_url}

    # -- tokens
    def set_access(self, token: str, until: float) -> None:
        """Use a token handed over by another process (worklog worker)."""
        self._access = token
        self._access_until = float(until)

    @property
    def access_until(self) -> float:
        return self._access_until

    def drop_access(self) -> None:
        self._access = None
        self._access_until = 0.0

    def _access_token(self) -> str:
        """The one door: cached token, else owner check + refresh."""
        if self._access and time.time() < self._access_until:
            return self._access
        self.cfg = load_config()
        self.check_owner()
        refresh = get_refresh(self.kh_url)
        if not refresh:
            raise NotSignedIn("nobody is signed in to Knowledge Hub on this machine - ask the person to run: kh login")
        try:
            r = self.http.post(self.token_url(), data={
                "grant_type": "refresh_token", "refresh_token": refresh, "client_id": self.client_id(),
            }, timeout=HTTP_TIMEOUT)
        except requests.RequestException:
            raise
        if r.status_code != 200:
            raise NotSignedIn("the KH sign-in has ended or was revoked - ask the person to run: kh login")
        tok = r.json()
        if tok.get("refresh_token") and tok["refresh_token"] != refresh:
            set_refresh(self.kh_url, tok["refresh_token"])
        self._access = tok.get("access_token")
        self._access_until = time.time() + int(tok.get("expires_in") or 300) - 20
        if not self._access:
            raise NotSignedIn("the KH sign-in has ended or was revoked - ask the person to run: kh login")
        return self._access

    def access_token(self) -> str:
        return self._access_token()

    # -- logout
    def _forget(self, url: str) -> str:
        cfg = load_config()
        refresh = get_refresh(url)
        result = "none"
        if refresh:
            result = "forgotten"
            try:
                iss = (cfg.get("issuers") or {}).get(url) or (cfg.get("issuer") if cfg.get("kh_url") == url else None)
                if iss:
                    r = self.http.post(f"{iss.rstrip('/')}/protocol/openid-connect/revoke", data={
                        "token": refresh, "token_type_hint": "refresh_token",
                        "client_id": cfg.get("client_id") or DEFAULT_CLIENT_ID,
                    }, timeout=HTTP_TIMEOUT)
                    if r.status_code < 300:
                        result = "revoked"
            except Exception:  # noqa: BLE001
                result = "forgotten"
            delete_refresh(url)
        cfg["signed_in_to"] = [u for u in (cfg.get("signed_in_to") or []) if u != url]
        if cfg.get("kh_url", "").rstrip("/") == url:
            cfg.pop("signed_in", None)
        save_config(cfg)
        self.cfg = cfg
        if url == self.kh_url:
            self.drop_access()
        return result

    def logout(self) -> str:
        return self._forget(self.kh_url)

    def logout_all(self) -> dict:
        cfg = load_config()
        urls: list[str] = []
        for u in list(cfg.get("signed_in_to") or []) + [cfg.get("kh_url"), self.kh_url, DEFAULT_KH_URL]:
            if u and u.rstrip("/") not in urls:
                urls.append(u.rstrip("/"))
        return {u: self._forget(u) for u in urls}

    # -- status
    def status(self) -> dict:
        self.cfg = load_config()
        self.check_owner()
        si = self.cfg.get("signed_in") if self.cfg.get("kh_url", "").rstrip("/") == self.kh_url else None
        si = si or {}
        return {
            "kh_url": self.kh_url, "signed_in": bool(get_refresh(self.kh_url)), "email": si.get("email"),
            "until": si.get("until"), "home": home(), "owner": owner.owner_of(home(), self.cfg),
        }

    # -- calls
    def headers(self) -> dict:
        h = {"Authorization": f"Bearer {self._access_token()}"}
        acting = self.acting_token if self.acting_token is not None else acting_from_env()
        if acting:
            h["X-KH-Acting-Token"] = acting
        actor = actor_from_env(self.actor)
        if actor:
            h["X-KH-Actor"] = actor
        return h

    def _send(self, method: str, url: str, **kw) -> requests.Response:
        """Retry once after 1.5 s only when no connection was made."""
        try:
            return self.http.request(method, url, timeout=HTTP_TIMEOUT, **kw)
        except requests.exceptions.ConnectTimeout:
            pass
        except requests.exceptions.ConnectionError as e:
            if not _no_connection(e):
                raise
        time.sleep(1.5)
        return self.http.request(method, url, timeout=HTTP_TIMEOUT, **kw)

    @staticmethod
    def _parse(r: requests.Response):
        ctype = r.headers.get("content-type", "")
        if "json" in ctype:
            try:
                return r.json()
            except ValueError:
                return r.text
        try:
            return r.json() if r.text[:1] in ("{", "[") else r.text
        except ValueError:
            return r.text

    def _call_url(self, method: str, url: str, body=None, params=None):
        kw: dict = {}
        if params:
            kw["params"] = params
        if body is not None:
            kw["json"] = body
        r = None
        for attempt in range(2):
            r = self._send(method.upper(), url, headers=self.headers(), **kw)
            if r.status_code == 401 and attempt == 0:
                self.drop_access()
                continue
            break
        assert r is not None
        data = self._parse(r)
        if isinstance(data, dict) and data.get("token_refresh_needed"):
            self.drop_access()
        return r.status_code, data

    def call(self, method: str, path: str, body=None, params=None):
        """KH call -> (status, parsed JSON or text)."""
        return self._call_url(method, self.kh_url + _unmangle(path), body, params)

    def call_identity(self, method: str, path: str, body=None):
        """Keycloak's KH extension at <issuer>/kh/<path>, same headers."""
        return self._call_url(method, f"{self.issuer()}/kh/{path.lstrip('/')}", body)


def _no_connection(e: Exception) -> bool:
    seen = set()
    cur: BaseException | None = e
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        name = type(cur).__name__
        if name in ("NewConnectionError", "NameResolutionError", "ConnectTimeoutError"):
            return True
        for a in getattr(cur, "args", ()) or ():
            if isinstance(a, BaseException):
                if _no_connection(a):
                    return True
            elif "NewConnectionError" in str(a) or "11001" in str(a):
                return True
        cur = cur.__cause__ or cur.__context__ or getattr(cur, "reason", None)
    return False
