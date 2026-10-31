"""KH client for the worker (spec §4.6, `khclient.py` + `person.py`).

Token providers:
- `StaticToken` (KH_WORKER_TOKEN, dev);
- `PrincipalAuth` (default `principal`): `POST /v1/token` client_credentials -> kh_people namespace token, then
  `POST /v1/kh_people/principals {role: distiller, principal_id: worker}` (409 = exists) and
  `POST /v1/kh_people/principals/worker/token` -> 15-min worker token, re-minted 90 s before expiry;
- `PersonAuth` (`person`, sidecar): password grant on a confidential Keycloak client, refresh token reused.

Ledgers (leases, month spend, run events):
- `KHLedger`: `/v1/kh_people/leases/<name>:acquire|renew|release`, `/v1/kh_people/metrics`,
  `/v1/kh_people/metrics:aggregate`, `/v1/kh_people/events` (principal identity);
- `FileLedger`: files in KH_WORKER_STATE_DIR (`lease.json`, `spend.jsonl`, `runs.jsonl`) (person identity).
Client-side dry enforcement: in dry mode every submit gets `dry_run: true`.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone


class KHError(Exception):
    def __init__(self, status: int, body):
        super().__init__(f"KH answered {status}: {str(body)[:300]}")
        self.status = status
        self.body = body


class KHBudget(KHError):
    """KH's 429 {error: budget} (lock 3)."""


class StaticToken:
    def __init__(self, token: str):
        self.token = token

    def bearer(self, http) -> str:  # noqa: ARG002
        return self.token


class PrincipalAuth:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id, self.client_secret = client_id, client_secret
        self._token, self._exp = None, 0.0

    def bearer(self, http) -> str:
        if self._token and time.time() < self._exp - 90:
            return self._token
        r = http.raw("POST", "/v1/token", {"grant_type": "client_credentials", "client_id": self.client_id,
                                           "client_secret": self.client_secret}, auth=None)
        ns_tok = r["access_token"]
        try:
            http.raw("POST", "/v1/kh_people/principals", {"role": "distiller", "principal_id": "worker"}, auth=ns_tok)
        except KHError as e:
            if e.status != 409:
                raise
        w = http.raw("POST", "/v1/kh_people/principals/worker/token", {}, auth=ns_tok)
        self._token = w.get("access_token") or w.get("token")
        self._exp = time.time() + float(w.get("expires_in") or 900)
        return self._token


class PersonAuth:
    def __init__(self, token_url: str, client_id: str, client_secret: str, username: str, password: str,
                 session=None):
        self.token_url, self.client_id, self.client_secret = token_url, client_id, client_secret
        self.username, self.password = username, password
        self._token, self._refresh, self._exp = None, None, 0.0
        self._session = session

    def _post(self, data: dict) -> dict:
        if self._session is not None:
            r = self._session.post(self.token_url, data=data, timeout=30)
        else:
            import requests

            r = requests.post(self.token_url, data=data, timeout=30)
        if r.status_code >= 300:
            raise KHError(r.status_code, r.text)
        return r.json()

    def bearer(self, http) -> str:  # noqa: ARG002
        if self._token and time.time() < self._exp - 60:
            return self._token
        base = {"client_id": self.client_id, "client_secret": self.client_secret}
        tok = None
        if self._refresh:
            try:
                tok = self._post({**base, "grant_type": "refresh_token", "refresh_token": self._refresh})
            except KHError:
                tok = None
        if tok is None:
            tok = self._post({**base, "grant_type": "password", "username": self.username, "password": self.password})
        self._token = tok["access_token"]
        self._refresh = tok.get("refresh_token")
        self._exp = time.time() + float(tok.get("expires_in") or 300)
        return self._token


class KHClient:
    def __init__(self, base_url: str = "", auth=None, session=None, timeout_s: float | None = None,
                 mode: str = "dry"):
        self.base_url = (base_url or "").rstrip("/")
        self.auth = auth
        self.session = session
        self.timeout_s = timeout_s or float(os.environ.get("KH_WORKER_HTTP_TIMEOUT_S") or 180)
        self.mode = mode

    def raw(self, method: str, path: str, body=None, params=None, auth="__default__"):
        headers = {"X-KH-Actor": "kh-worker"}
        tok = self.auth.bearer(self) if (auth == "__default__" and self.auth is not None) else (
            auth if auth not in (None, "__default__") else None)
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        url = self.base_url + path
        if self.session is not None:
            r = self.session.request(method, url, json=body, params=params, headers=headers, timeout=self.timeout_s)
        else:
            import requests

            r = requests.request(method, url, json=body, params=params, headers=headers, timeout=self.timeout_s)
        try:
            data = r.json()
        except ValueError:
            data = {"text": getattr(r, "text", "")}
        if r.status_code == 429 and isinstance(data, dict) and data.get("error") == "budget":
            raise KHBudget(429, data)
        if r.status_code >= 300:
            raise KHError(r.status_code, data)
        return data

    # -- KH routes used by the jobs -----------------------------------------------------------------------
    def distill_next(self, project: str, limit: int | None = None) -> dict:
        params = {"project": project}
        if limit:
            params["limit"] = limit
        return self.raw("GET", "/distill/next", params=params)

    def _dry(self, body: dict) -> dict:
        b = dict(body)
        if self.mode != "on":
            b["dry_run"] = True
        return b

    def distill_submit(self, body: dict) -> dict:
        return self.raw("POST", "/distill/submit", self._dry(body))

    def distill_approve(self, project: str, ids: list, share: list | None = None) -> dict:
        return self.raw("POST", "/distill/approve", {"project": project, "ids": ids, "share": share or []})

    def distill_patterns(self, days: int = 1) -> dict:
        return self.raw("GET", "/distill/patterns", params={"days": days})

    def promote_next(self) -> dict:
        return self.raw("GET", "/promote/next")

    def promote_submit(self, body: dict) -> dict:
        return self.raw("POST", "/promote/submit", self._dry(body))

    def daily_next(self, project: str) -> dict:
        return self.raw("GET", "/daily/next", params={"project": project})

    def daily_submit(self, body: dict) -> dict:
        return self.raw("POST", "/daily/submit", self._dry(body))

    def session_sweep(self) -> dict:
        return self.raw("POST", "/session/sweep", {})


# ---------------------------------------------------------------------------
# Leases and ledgers
# ---------------------------------------------------------------------------


class Lease:
    """A long lease held as short grants (KH clamps to 5..300 s), renewed every ttl/3 by a thread."""

    def __init__(self, ledger, name: str, grant_s: int = 300):
        self.ledger, self.name, self.grant_s = ledger, name, grant_s
        self.lost = False
        self.epoch = None
        self._stop = threading.Event()
        self._t = None

    def acquire(self) -> bool:
        self.epoch = self.ledger.lease_acquire(self.name, self.grant_s)
        if self.epoch is None:
            return False
        self._t = threading.Thread(target=self._renew, daemon=True, name=f"lease-{self.name}")
        self._t.start()
        return True

    def _renew(self):
        while not self._stop.wait(self.grant_s / 3):
            try:
                if not self.ledger.lease_renew(self.name, self.epoch, self.grant_s):
                    self.lost = True
                    return
            except Exception:  # noqa: BLE001
                self.lost = True
                return

    def release(self):
        self._stop.set()
        try:
            self.ledger.lease_release(self.name, self.epoch)
        except Exception:  # noqa: BLE001
            pass


def _month_bounds() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start.isoformat(), nxt.isoformat()


class KHLedger:
    NS = "/v1/kh_people"

    def __init__(self, kh: KHClient):
        self.kh = kh
        self.holder = f"worker-{uuid.uuid4().hex[:8]}"

    def lease_acquire(self, name: str, ttl: int):
        try:
            r = self.kh.raw("POST", f"{self.NS}/leases/{name}:acquire", {"holder": self.holder, "ttl_s": ttl})
        except KHError as e:
            if e.status in (409, 423):
                return None
            raise
        return r.get("epoch", 1)

    def lease_renew(self, name: str, epoch, ttl: int) -> bool:
        try:
            self.kh.raw("POST", f"{self.NS}/leases/{name}:renew", {"holder": self.holder, "epoch": epoch, "ttl_s": ttl})
            return True
        except KHError:
            return False

    def lease_release(self, name: str, epoch) -> None:
        self.kh.raw("POST", f"{self.NS}/leases/{name}:release", {"holder": self.holder, "epoch": epoch})

    def month_spend(self) -> float:
        frm, to = _month_bounds()
        r = self.kh.raw("GET", f"{self.NS}/metrics:aggregate", params={"series": "worker.cost_usd",
                                                                       "group_by": "dims.kind", "from": frm, "to": to})
        total = 0.0
        for g in r.get("groups") or []:
            if (g.get("dims.kind") or g.get("kind")) == "backfill":
                continue
            total += float(g.get("sum") or g.get("value") or 0)
        return total

    def record_spend(self, usd: float, kind: str = "run", info: dict | None = None) -> None:
        self.kh.raw("POST", f"{self.NS}/metrics", {"series": "worker.cost_usd", "value": usd,
                                                  "dims": {"kind": kind, **{k: str(v) for k, v in (info or {}).items()
                                                                           if k == "model"}}})

    def event(self, kind: str, body: dict) -> None:
        self.kh.raw("POST", f"{self.NS}/events", {"events": [{"kind": kind, "body": body}]})


class FileLedger:
    def __init__(self, state_dir: str):
        self.dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.holder = f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._lock = threading.Lock()

    def _path(self, n: str) -> str:
        return os.path.join(self.dir, n)

    def _leases(self) -> dict:
        try:
            with open(self._path("lease.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self, d: dict) -> None:
        with open(self._path("lease.json"), "w", encoding="utf-8") as f:
            json.dump(d, f)

    def lease_acquire(self, name: str, ttl: int):
        with self._lock:
            d = self._leases()
            cur = d.get(name)
            now = time.time()
            if cur and cur.get("until", 0) > now and cur.get("holder") != self.holder:
                return None
            epoch = (cur or {}).get("epoch", 0) + (0 if cur and cur.get("holder") == self.holder else 1)
            d[name] = {"holder": self.holder, "epoch": epoch, "until": now + ttl}
            self._save(d)
            return epoch

    def lease_renew(self, name: str, epoch, ttl: int) -> bool:
        with self._lock:
            d = self._leases()
            cur = d.get(name) or {}
            if cur.get("holder") != self.holder or cur.get("epoch") != epoch:
                return False
            cur["until"] = time.time() + ttl
            d[name] = cur
            self._save(d)
            return True

    def lease_release(self, name: str, epoch) -> None:
        with self._lock:
            d = self._leases()
            cur = d.get(name) or {}
            if cur.get("holder") == self.holder:
                cur["until"] = 0
                d[name] = cur
                self._save(d)

    def _append(self, n: str, row: dict) -> None:
        with self._lock, open(self._path(n), "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def _read(self, n: str) -> list[dict]:
        try:
            with open(self._path(n), encoding="utf-8") as f:
                return [json.loads(x) for x in f if x.strip()]
        except OSError:
            return []

    def month_spend(self) -> float:
        frm, _to = _month_bounds()
        return sum(float(r.get("usd") or 0) for r in self._read("spend.jsonl")
                   if r.get("kind") != "backfill" and str(r.get("ts") or "") >= frm)

    def record_spend(self, usd: float, kind: str = "run", info: dict | None = None) -> None:
        self._append("spend.jsonl", {"ts": datetime.now(timezone.utc).isoformat(), "usd": usd, "kind": kind,
                                     "model": (info or {}).get("model")})

    def event(self, kind: str, body: dict) -> None:
        self._append("runs.jsonl", {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, "body": body})

    def events(self) -> list[dict]:
        return self._read("runs.jsonl")
