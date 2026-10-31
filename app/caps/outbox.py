"""outbox (spec §6.14): webhooks for manifest subscriptions (fan-out from the event stream, signed delivery with
retries) plus a pull/ack API.

Fan-out (cron `outbox_fanout`, 2 s): per subscription, lock the cursor row, read events after `last_seq`, insert one
`kh_outbox` row per matching event (PK (ns, delivery_id) makes repeats no-ops), move the cursor - one transaction.
Delivery (cron `outbox_deliver`, 2 s): conditional claim with a token, POST with an HMAC-SHA256 signature, 2xx done,
else exponential backoff, dead after 12 attempts. SSRF guard on every target. HTTP goes through `http_post` (tests
monkeypatch it).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import os
import secrets
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Query, Request

from app import sqldb
from app.caps import core
from app.caps.core import CapError, Ctx

log = logging.getLogger(__name__)

NAME = "outbox"
VERSION = "1.0.0"
STATUS = "beta"
DEFAULT_ON = True
CLAIM_S = 30
CLAIM_ROWS = 24
WORKERS = 8
POST_TIMEOUT_S = 10
MAX_ATTEMPTS = 12
BACKOFF_BASE_S = 1
BACKOFF_CAP_S = 3600
FANOUT_BATCH = 500
PULL_MAX = 1000
WAIT_MAX = 25
DONE_KEEP_DAYS = 7
VERIFY_TOLERANCE_S = 300
POLL_S = 0.25
LIMITS = {"claim_s": CLAIM_S, "rows_per_claim": CLAIM_ROWS, "workers": WORKERS, "post_timeout_s": POST_TIMEOUT_S,
          "attempts": MAX_ATTEMPTS, "backoff_cap_s": BACKOFF_CAP_S, "fanout_batch": FANOUT_BATCH,
          "pull": PULL_MAX, "long_poll_s": WAIT_MAX, "done_keep_days": DONE_KEEP_DAYS}
TABLES = ["kh_outbox", "kh_outbox_cursor", "kh_outbox_secret"]
LOCAL_TABLES: list[str] = []
STATES = ("pending", "claimed", "done", "dead")


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _m1(c) -> None:
    sqldb.create_table(
        c, "kh_outbox",
        "CREATE TABLE IF NOT EXISTS kh_outbox (ns TEXT NOT NULL, delivery_id TEXT NOT NULL, sub_id TEXT NOT NULL, "
        "event_seq INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_ms INTEGER NOT NULL DEFAULT 0, "
        "state TEXT NOT NULL DEFAULT 'pending', claim_token TEXT, claimed_until_ms INTEGER, last_status INTEGER, "
        "last_error TEXT, created_ms INTEGER, done_ms INTEGER, PRIMARY KEY (ns, delivery_id))",
        "CREATE COLUMN TABLE kh_outbox (ns NVARCHAR(64) NOT NULL, delivery_id NVARCHAR(128) NOT NULL, "
        "sub_id NVARCHAR(64) NOT NULL, event_seq BIGINT NOT NULL, attempts INTEGER NOT NULL, next_ms BIGINT NOT NULL, "
        "state NVARCHAR(16) NOT NULL, claim_token NVARCHAR(64), claimed_until_ms BIGINT, last_status INTEGER, "
        "last_error NVARCHAR(1024), created_ms BIGINT, done_ms BIGINT, PRIMARY KEY (ns, delivery_id))")
    sqldb.create_index(c, "kh_outbox_due", "kh_outbox", ["state", "next_ms"])
    sqldb.create_table(
        c, "kh_outbox_cursor",
        "CREATE TABLE IF NOT EXISTS kh_outbox_cursor (ns TEXT NOT NULL, sub_id TEXT NOT NULL, "
        "last_seq INTEGER NOT NULL DEFAULT 0, pull_seq INTEGER NOT NULL DEFAULT 0, rev INTEGER NOT NULL DEFAULT 1, "
        "PRIMARY KEY (ns, sub_id))",
        "CREATE COLUMN TABLE kh_outbox_cursor (ns NVARCHAR(64) NOT NULL, sub_id NVARCHAR(64) NOT NULL, "
        "last_seq BIGINT NOT NULL, pull_seq BIGINT NOT NULL, rev BIGINT NOT NULL, PRIMARY KEY (ns, sub_id))")
    sqldb.create_table(
        c, "kh_outbox_secret",
        "CREATE TABLE IF NOT EXISTS kh_outbox_secret (ns TEXT NOT NULL, sub_id TEXT NOT NULL, secret TEXT NOT NULL, "
        "created_ms INTEGER, PRIMARY KEY (ns, sub_id))",
        "CREATE COLUMN TABLE kh_outbox_secret (ns NVARCHAR(64) NOT NULL, sub_id NVARCHAR(64) NOT NULL, "
        "secret NVARCHAR(256) NOT NULL, created_ms BIGINT, PRIMARY KEY (ns, sub_id))")


def init(c) -> None:
    sqldb.migrate(c, NAME, [(1, _m1)])
    if _on_manifest not in core.ON_MANIFEST:
        core.ON_MANIFEST.append(_on_manifest)
    if _on_state not in core.ON_STATE:
        core.ON_STATE.append(_on_state)


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def stream_end(c, ns: str) -> int:
    return sqldb.counter_get(c, f"ev:{ns}")


def _ensure_cursor(c, ns: str, sub_id: str) -> dict:
    r = c.execute("SELECT last_seq, pull_seq, rev FROM kh_outbox_cursor WHERE ns=? AND sub_id=?",
                  (ns, sub_id)).fetchone()
    if r is not None:
        return {"last_seq": int(r[0]), "pull_seq": int(r[1]), "rev": int(r[2])}
    end = stream_end(c, ns)
    sqldb.insert_new(c, "kh_outbox_cursor", {"ns": ns, "sub_id": sub_id, "last_seq": end, "pull_seq": end, "rev": 1})
    r = c.execute("SELECT last_seq, pull_seq, rev FROM kh_outbox_cursor WHERE ns=? AND sub_id=?",
                  (ns, sub_id)).fetchone()
    return {"last_seq": int(r[0]), "pull_seq": int(r[1]), "rev": int(r[2])}


def _new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def _on_manifest(c, ns: str, parsed: dict, _old: dict | None) -> None:
    """New subscription: cursor at the current stream end, KH secret unless secret_ref. Removed: cursor and
    pending rows go away (dead `unsubscribed`)."""
    subs = parsed.get("subscriptions") or {}
    now = sqldb.now_ms(c)
    for sid, s in subs.items():
        _ensure_cursor(c, ns, sid)
        if not s.get("secret_ref"):
            sqldb.insert_new(c, "kh_outbox_secret", {"ns": ns, "sub_id": sid, "secret": _new_secret(),
                                                     "created_ms": now})
    for r in c.execute("SELECT sub_id FROM kh_outbox_cursor WHERE ns=?", (ns,)).fetchall():
        if r[0] not in subs:
            c.execute("DELETE FROM kh_outbox_cursor WHERE ns=? AND sub_id=?", (ns, r[0]))
            c.execute("DELETE FROM kh_outbox_secret WHERE ns=? AND sub_id=?", (ns, r[0]))
            c.execute("UPDATE kh_outbox SET state='dead', last_error='unsubscribed', claim_token=NULL "
                      "WHERE ns=? AND sub_id=? AND state IN ('pending','claimed')", (ns, r[0]))


def _on_state(c, ns: str, src: str, dst: str) -> None:
    if dst in ("detached", "purged"):
        c.execute("UPDATE kh_outbox SET state='dead', last_error='detached', claim_token=NULL "
                  "WHERE ns=? AND state IN ('pending','claimed')", (ns,))
    elif src == "detached" and dst == "active":
        c.execute(f"UPDATE kh_outbox SET state='pending', last_error=NULL, next_ms={sqldb.now_ms_sql(c)} "
                  "WHERE ns=? AND state='dead' AND last_error='detached'", (ns,))


# ---------------------------------------------------------------------------
# Kind filter, signature
# ---------------------------------------------------------------------------


def kind_matches(patterns: list[str], kind: str) -> bool:
    for p in patterns or []:
        if p == "*" or p == kind:
            return True
        if p.endswith(".*") and kind.startswith(p[:-1]):
            return True
    return False


def sign(secret: str, ts_ms: int | str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{ts_ms}.".encode("utf-8") + body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def verify(secret: str, body: bytes, timestamp: str | int, signature: str, tolerance_s: int = VERIFY_TOLERANCE_S,
           now_ms: int | None = None) -> bool:
    """Receiver-side check: signature matches and the timestamp (ms) is within `tolerance_s`."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    now = core.now_ms() if now_ms is None else now_ms
    if abs(now - ts) > tolerance_s * 1000:
        return False
    return hmac.compare_digest(sign(secret, ts, body), signature or "")


def backoff_ms(attempts: int) -> int:
    return int(min(BACKOFF_BASE_S * (2 ** max(0, attempts - 1)), BACKOFF_CAP_S) * 1000)


# ---------------------------------------------------------------------------
# SSRF guard + HTTP
# ---------------------------------------------------------------------------


class UnsafeTarget(Exception):
    pass


def allow_local() -> bool:
    return (os.environ.get("KH_OUTBOX_ALLOW_LOCAL") or "").strip() == "1"


def _global(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or not ip.is_global):
        return False
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        return False
    if str(ip) in ("169.254.169.254", "fd00:ec2::254"):
        return False
    return True


def resolve_target(url: str, hosts_allow: list[str] | None = None) -> tuple[str, str, int, str, str]:
    """-> (scheme, host, port, ip, path). Raises UnsafeTarget."""
    try:
        u = urlsplit(url or "")
    except ValueError:
        raise UnsafeTarget("bad url")
    host = (u.hostname or "").lower()
    if not host:
        raise UnsafeTarget("no host")
    local = u.scheme == "http" and host in ("127.0.0.1", "localhost") and allow_local()
    if u.scheme != "https" and not local:
        raise UnsafeTarget("https only")
    if hosts_allow and host not in {h.lower() for h in hosts_allow}:
        raise UnsafeTarget(f"host '{host}' is not in settings.webhook_hosts")
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise UnsafeTarget(f"cannot resolve: {e}")
    addrs = list(dict.fromkeys(i[4][0] for i in infos))
    if not addrs:
        raise UnsafeTarget("no address")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not local and not _global(ip):
            raise UnsafeTarget(f"address {a} is not global")
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    return u.scheme, host, port, addrs[0], path


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, ip: str, host: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):  # connect to the resolved address, SNI/cert for the host name
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, ip: str, host: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)


def http_post(url: str, body: bytes, headers: dict, timeout: float, hosts_allow: list[str] | None = None) -> int:
    """POST without redirects to the pinned, checked address. Returns the HTTP status. Raises UnsafeTarget or
    OSError/HTTPException on transport failure."""
    scheme, host, port, ip, path = resolve_target(url, hosts_allow)
    cls = _PinnedHTTPS if scheme == "https" else _PinnedHTTP
    conn = cls(ip, host, port, timeout)
    try:
        conn.request("POST", path, body=body, headers={**headers, "Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read(65536)
        return int(resp.status)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


def _active_ns(c) -> list[str]:
    if not sqldb.table_exists(c, "kh_ns"):
        return []
    return [r[0] for r in c.execute("SELECT ns FROM kh_ns WHERE state='active'").fetchall()]


def fanout_sub(ns: str, sub_id: str, kinds: list[str]) -> int:
    """One transaction: lock cursor (CAS on rev), insert rows for matching events, move cursor."""
    with sqldb.connect() as c:
        cur = _ensure_cursor(c, ns, sub_id)
        moved = c.execute("UPDATE kh_outbox_cursor SET rev=rev+1 WHERE ns=? AND sub_id=? AND rev=?",
                          (ns, sub_id, cur["rev"]))
        if not moved.rowcount or moved.rowcount <= 0:
            return 0
        rows = c.execute(f"SELECT seq, kind FROM kh_event WHERE ns=? AND seq>? ORDER BY seq LIMIT {FANOUT_BATCH}",
                         (ns, cur["last_seq"])).fetchall()
        if not rows:
            return 0
        now = sqldb.now_ms(c)
        n = 0
        for seq, kind in rows:
            if not kind_matches(kinds, kind):
                continue
            if sqldb.insert_new(c, "kh_outbox", {"ns": ns, "delivery_id": f"{sub_id}:{int(seq)}", "sub_id": sub_id,
                                                 "event_seq": int(seq), "attempts": 0, "next_ms": now,
                                                 "state": "pending", "created_ms": now}):
                n += 1
        c.execute("UPDATE kh_outbox_cursor SET last_seq=? WHERE ns=? AND sub_id=?", (int(rows[-1][0]), ns, sub_id))
        return n


def cron_fanout() -> int:
    with sqldb.connect() as c:
        nss = _active_ns(c)
        subs = []
        for ns in nss:
            for r in c.execute("SELECT sub_id, kinds_json FROM kh_subscription WHERE ns=? AND state='active' "
                               "AND url IS NOT NULL", (ns,)).fetchall():
                subs.append((ns, r[0], core.loads(r[1], []) or []))
    total = 0
    for ns, sid, kinds in subs:
        try:
            total += fanout_sub(ns, sid, kinds)
        except Exception as e:  # noqa: BLE001
            log.warning("outbox fan-out %s/%s failed: %s", ns, sid, e)
    return total


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def secret_for(c, ns: str, sub_id: str, secret_ref: str | None) -> str | None:
    if secret_ref:
        if secret_ref.startswith("env:"):
            return os.environ.get(secret_ref[4:]) or None
        return None
    r = c.execute("SELECT secret FROM kh_outbox_secret WHERE ns=? AND sub_id=?", (ns, sub_id)).fetchone()
    return r[0] if r else None


def claim(limit: int = CLAIM_ROWS) -> list[dict]:
    token = core.new_id()
    out = []
    with sqldb.connect() as c:
        nss = _active_ns(c)
        if not nss:
            return []
        now = sqldb.now_ms(c)
        ph = ", ".join("?" for _ in nss)
        cands = c.execute(
            f"SELECT ns, delivery_id FROM kh_outbox WHERE ns IN ({ph}) AND ((state='pending' AND next_ms<=?) OR "
            f"(state='claimed' AND claimed_until_ms<?)) ORDER BY next_ms LIMIT {int(limit)}",
            [*nss, now, now]).fetchall()
        for ns, did in cands:
            cur = c.execute("UPDATE kh_outbox SET state='claimed', claim_token=?, claimed_until_ms=? WHERE ns=? AND "
                            "delivery_id=? AND ((state='pending' AND next_ms<=?) OR (state='claimed' AND "
                            "claimed_until_ms<?))", (token, now + CLAIM_S * 1000, ns, did, now, now))
            if cur.rowcount and cur.rowcount > 0:
                r = c.execute("SELECT * FROM kh_outbox WHERE ns=? AND delivery_id=?", (ns, did)).fetchone()
                out.append({k: r[k] for k in r.keys()})
    return out


def _finish(row: dict, status: int | None, error: str | None, dead_now: bool = False) -> str:
    ok = status is not None and 200 <= status < 300
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        attempts = int(row["attempts"]) + 1
        if ok:
            state, nxt = "done", row["next_ms"]
        elif dead_now or attempts >= MAX_ATTEMPTS:
            state, nxt = "dead", row["next_ms"]
        else:
            state, nxt = "pending", now + backoff_ms(attempts)
        c.execute("UPDATE kh_outbox SET state=?, attempts=?, next_ms=?, last_status=?, last_error=?, claim_token=NULL, "
                  "claimed_until_ms=NULL, done_ms=? WHERE ns=? AND delivery_id=? AND claim_token=? AND state='claimed'",
                  (state, attempts, nxt, status, (error or None) and str(error)[:1000], now if ok else None,
                   row["ns"], row["delivery_id"], row["claim_token"]))
    return state


def deliver_one(row: dict) -> str:
    ns, sid = row["ns"], row["sub_id"]
    from app.eventing.store import decode

    with sqldb.connect() as c:
        sub = c.execute("SELECT url, secret_ref, state FROM kh_subscription WHERE ns=? AND sub_id=?",
                        (ns, sid)).fetchone()
        ev = c.execute("SELECT * FROM kh_event WHERE ns=? AND seq=?", (ns, int(row["event_seq"]))).fetchone()
        secret = secret_for(c, ns, sid, sub["secret_ref"]) if sub is not None else None
        st = core.load_ns(c, ns)
    if sub is None or not sub["url"]:
        return _finish(row, None, "unsubscribed", dead_now=True)
    if ev is None:
        return _finish(row, None, "event_gone", dead_now=True)
    if not secret:
        return _finish(row, None, "secret_missing")
    hosts = (st.settings().get("webhook_hosts") if st else None) or None
    payload = {"delivery_id": row["delivery_id"], "ns": ns, "sub_id": sid, "event": decode(ev)}
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    ts = core.now_ms()
    headers = {"X-KH-Delivery": row["delivery_id"], "X-KH-Namespace": ns, "X-KH-Subscription": sid,
               "X-KH-Event-Seq": str(int(row["event_seq"])), "X-KH-Timestamp": str(ts),
               "X-KH-Signature": sign(secret, ts, body)}
    try:
        status = http_post(sub["url"], body, headers, POST_TIMEOUT_S, hosts)
    except UnsafeTarget as e:
        return _finish(row, None, f"unsafe_target: {e}", dead_now=True)
    except Exception as e:  # noqa: BLE001
        return _finish(row, None, f"{type(e).__name__}: {e}")
    return _finish(row, status, None if 200 <= status < 300 else f"HTTP {status}")


def cron_deliver() -> dict:
    rows = claim()
    if not rows:
        return {}
    counts: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for st in pool.map(deliver_one, rows):
            counts[st] = counts.get(st, 0) + 1
    return counts


def cron_gc() -> int:
    with sqldb.connect() as c:
        if not sqldb.table_exists(c, "kh_outbox"):
            return 0
        cut = sqldb.now_ms(c) - DONE_KEEP_DAYS * 86_400_000
        cur = c.execute("DELETE FROM kh_outbox WHERE state='done' AND done_ms<?", (cut,))
        return int(cur.rowcount or 0)


CRONS = [("outbox_fanout", 2, cron_fanout), ("outbox_deliver", 2, cron_deliver), ("outbox_gc", 3600, cron_gc)]


# ---------------------------------------------------------------------------
# Route helpers
# ---------------------------------------------------------------------------


def _sub(c, ns: str, sub_id: str) -> dict:
    r = c.execute("SELECT * FROM kh_subscription WHERE ns=? AND sub_id=?", (ns, sub_id)).fetchone()
    if r is None:
        raise CapError("not_found", f"subscription '{sub_id}' not found")
    d = {k: r[k] for k in r.keys()}
    d["kinds"] = core.loads(d.pop("kinds_json", None), []) or []
    return d


def list_subs(ns: str) -> list[dict]:
    out = []
    with sqldb.connect() as c:
        for r in c.execute("SELECT * FROM kh_subscription WHERE ns=? ORDER BY sub_id", (ns,)).fetchall():
            sid = r["sub_id"]
            cur = c.execute("SELECT last_seq, pull_seq FROM kh_outbox_cursor WHERE ns=? AND sub_id=?",
                            (ns, sid)).fetchone()
            counts = {s: 0 for s in STATES}
            for s, n in c.execute("SELECT state, COUNT(*) FROM kh_outbox WHERE ns=? AND sub_id=? GROUP BY state",
                                  (ns, sid)).fetchall():
                counts[s] = int(n)
            out.append({"sub_id": sid, "kinds": core.loads(r["kinds_json"], []) or [], "url": r["url"],
                        "state": r["state"], "secret_ref": r["secret_ref"],
                        "secret": "external" if r["secret_ref"] else "kh",
                        "last_seq": int(cur[0]) if cur else None, "acked_seq": int(cur[1]) if cur else None,
                        "deliveries": counts})
    return out


def get_secret(ns: str, sub_id: str, rotate: bool) -> dict:
    with sqldb.connect() as c:
        sub = _sub(c, ns, sub_id)
        if sub.get("secret_ref"):
            raise CapError("secret_external", f"subscription '{sub_id}' uses {sub['secret_ref']}; KH does not hold it",
                           status=409)
        now = sqldb.now_ms(c)
        if rotate:
            sqldb.upsert(c, "kh_outbox_secret", ["ns", "sub_id"], {"ns": ns, "sub_id": sub_id,
                                                                   "secret": _new_secret(), "created_ms": now})
        elif not sqldb.insert_new(c, "kh_outbox_secret", {"ns": ns, "sub_id": sub_id, "secret": _new_secret(),
                                                          "created_ms": now}):
            pass
        r = c.execute("SELECT secret, created_ms FROM kh_outbox_secret WHERE ns=? AND sub_id=?",
                      (ns, sub_id)).fetchone()
    if rotate:
        core.emit(ns, "kh.secret_rotated", {"sub_id": sub_id})
    return {"sub_id": sub_id, "secret": r[0], "created_ms": r[1], "rotated": rotate}


def list_outbox(ns: str, state: str | None, sub_id: str | None, limit: int) -> list[dict]:
    where, params = ["ns=?"], [ns]
    if state:
        if state not in STATES:
            raise CapError("invalid", f"state must be one of {', '.join(STATES)}")
        where.append("state=?")
        params.append(state)
    if sub_id:
        where.append("sub_id=?")
        params.append(sub_id)
    with sqldb.connect() as c:
        rows = c.execute(f"SELECT * FROM kh_outbox WHERE {' AND '.join(where)} ORDER BY created_ms DESC, event_seq DESC "
                         f"LIMIT {int(limit)}", params).fetchall()
        return [{k: r[k] for k in r.keys() if k not in ("ns", "claim_token")} for r in rows]


def retry(ns: str, delivery_id: str) -> dict:
    with sqldb.connect() as c:
        r = c.execute("SELECT state FROM kh_outbox WHERE ns=? AND delivery_id=?", (ns, delivery_id)).fetchone()
        if r is None:
            raise CapError("not_found", f"delivery '{delivery_id}' not found")
        if r[0] != "dead":
            raise CapError("invalid_transition", f"delivery '{delivery_id}' is {r[0]}; only dead deliveries retry",
                           extra={"state": r[0]})
        now = sqldb.now_ms(c)
        c.execute("UPDATE kh_outbox SET state='pending', attempts=0, next_ms=?, last_error=NULL, claim_token=NULL "
                  "WHERE ns=? AND delivery_id=? AND state='dead'", (now, ns, delivery_id))
        out = c.execute("SELECT * FROM kh_outbox WHERE ns=? AND delivery_id=?", (ns, delivery_id)).fetchone()
        return {k: out[k] for k in out.keys() if k not in ("ns", "claim_token")}


def pull_once(ctx: Ctx, sub_id: str, after_seq: int | None, limit: int) -> dict:
    from app.caps import events as ev_mod
    from app.eventing.store import COLUMNS, decode

    hidden = set(ev_mod.hidden_kinds(ctx))
    with sqldb.connect() as c:
        sub = _sub(c, ctx.ns, sub_id)
        cur = _ensure_cursor(c, ctx.ns, sub_id)
        start = cur["pull_seq"] if after_seq is None else int(after_seq)
        rows = c.execute(f"SELECT {', '.join(COLUMNS)} FROM kh_event WHERE ns=? AND seq>? ORDER BY seq "
                         f"LIMIT {PULL_MAX}", (ctx.ns, start)).fetchall()
    events, nxt = [], start
    for r in rows:
        if len(events) >= limit:
            break
        nxt = int(r["seq"])
        if r["kind"] in hidden or not kind_matches(sub["kinds"], r["kind"]):
            continue
        events.append(decode(r))
    return {"sub_id": sub_id, "events": events, "next_after_seq": nxt, "acked_seq": cur["pull_seq"]}


def ack(ns: str, sub_id: str, after_seq: int) -> dict:
    with sqldb.connect() as c:
        _sub(c, ns, sub_id)
        _ensure_cursor(c, ns, sub_id)
        end = stream_end(c, ns)
        if after_seq > end:
            raise CapError("invalid", f"after_seq {after_seq} is beyond the stream end {end}")
        c.execute("UPDATE kh_outbox_cursor SET pull_seq=?, rev=rev+1 WHERE ns=? AND sub_id=? AND pull_seq<?",
                  (after_seq, ns, sub_id, after_seq))
        r = c.execute("SELECT pull_seq FROM kh_outbox_cursor WHERE ns=? AND sub_id=?", (ns, sub_id)).fetchone()
    return {"sub_id": sub_id, "acked_seq": int(r[0])}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def attach(app: FastAPI) -> None:
    tag = ["v1 outbox"]

    @app.get("/v1/{ns}/subscriptions", tags=tag, summary="List webhook subscriptions")
    async def get_subs(ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require_admin()
        return {"subscriptions": await core.run(list_subs, ns)}

    @app.get("/v1/{ns}/subscriptions/{sub_id}/pull", tags=tag, summary="Pull a subscription's events")
    async def pull(ns: str, sub_id: str, after_seq: str | None = Query(None), limit: str | None = Query(None),
                   wait_s: float = Query(0), ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require("events", "read")
        lim = core.clamp_limit(limit, 100, PULL_MAX)
        after = core.parse_int(after_seq, "after_seq", None, 0)
        wait = max(0.0, min(float(WAIT_MAX), float(wait_s or 0)))
        deadline = time.monotonic() + wait
        while True:
            out = await core.run(pull_once, ctx, sub_id, after, lim)
            if out["events"] or time.monotonic() >= deadline:
                return out
            after = out["next_after_seq"]
            await asyncio.sleep(POLL_S)

    @app.post("/v1/{ns}/subscriptions/{sub_id}/ack", tags=tag, summary="Acknowledge pulled events")
    async def post_ack(request: Request, ns: str, sub_id: str, ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require("events", "read")
        body = await core.read_object(request)
        after = core.parse_int(body.get("after_seq"), "after_seq", None, 0)
        if after is None:
            raise CapError("invalid", "after_seq is required")
        return await core.run(ack, ns, sub_id, after)

    @app.get("/v1/{ns}/subscriptions/{sub_id}/secret", tags=tag, summary="Read a subscription's signing secret")
    async def get_sub_secret(ns: str, sub_id: str, ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require_admin()
        return await core.run(get_secret, ns, sub_id, False)

    @app.post("/v1/{ns}/subscriptions/{sub_id}/secret/rotate", tags=tag, summary="Rotate a signing secret")
    @core.idempotent
    async def rotate_secret(request: Request, ns: str, sub_id: str, ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require_admin()
        return core.reply(await core.run(get_secret, ns, sub_id, True))

    @app.get("/v1/{ns}/outbox", tags=tag, summary="List webhook deliveries")
    async def get_outbox(ns: str, state: str | None = Query(None), sub_id: str | None = Query(None),
                         limit: str | None = Query(None), ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require_admin()
        lim = core.clamp_limit(limit, 100, PULL_MAX)
        return {"deliveries": await core.run(list_outbox, ns, state, sub_id, lim)}

    @app.post("/v1/{ns}/outbox/{delivery_id}/retry", tags=tag, summary="Retry a dead delivery")
    async def post_retry(ns: str, delivery_id: str, ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require_admin()
        return await core.run(retry, ns, delivery_id)
