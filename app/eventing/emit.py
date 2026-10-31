"""KH event emitter (spec §1.15, §4.9).

`emit` never raises or blocks. It does nothing unless `KH_EVENTS=on`; then it builds the `kh_event` row and
appends it to an in-process outbox (deque capped at KH_EVENTS_OUTBOX_MAX, oldest dropped and counted).
A flusher thread (`kh-events-flusher`, every 1 s) hands batches of 500 to the EventBuffer (`app/eventing/queue.py`);
a failed batch is pushed back to the front. atexit drains up to 5 s. `ApiCallMiddleware` emits `kh.api.call`.
Routes live in `app/eventing/__init__.py` (`attach`).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque

from app import config

log = logging.getLogger(__name__)

_outbox: deque = deque()
_lock = threading.Lock()
stats = {"emitted": 0, "dropped": 0, "sent": 0, "send_errors": 0}


def enabled() -> bool:
    return (os.environ.get("KH_EVENTS") or ("on" if config.EVENTS_ON else "off")).strip().lower() == "on"


def new_id() -> str:
    return f"{int(time.time() * 1000):012x}"[-12:] + os.urandom(6).hex()


def build_row(kind: str, body: dict | None, project=None, email=None, actor=None, ix2=None, n1=None) -> dict:
    return {
        "id": new_id(),
        "kind": kind,
        "project": project,
        "ts_ms": int(time.time() * 1000),
        "session_id": (actor or "")[:128] or None,
        "actor_json": json.dumps({"email": email, "actor": actor}),
        "ix_s1": email,
        "ix_s2": ix2,
        "ix_n1": n1,
        "body_json": json.dumps(body or {}, ensure_ascii=False, default=str),
    }


def emit(kind: str, body: dict | None = None, project: str | None = None, email: str | None = None,
         actor: str | None = None, ix2: str | None = None, n1: float | None = None) -> None:
    try:
        if not enabled():
            return
        row = build_row(kind, body, project, email, actor, ix2, n1)
        with _lock:
            _outbox.append(row)
            stats["emitted"] += 1
            while len(_outbox) > max(1, config.EVENTS_OUTBOX_MAX):
                _outbox.popleft()
                stats["dropped"] += 1
    except Exception as e:  # noqa: BLE001 - never raise
        log.debug("emit failed: %s", e)


def drain(n: int = 500) -> list[dict]:
    """Take up to n rows from the outbox (for the §4 flusher)."""
    out = []
    with _lock:
        while _outbox and len(out) < n:
            out.append(_outbox.popleft())
    return out


def pending() -> int:
    return len(_outbox)


# ---------------------------------------------------------------------------
# Flusher (§4.9)
# ---------------------------------------------------------------------------

BATCH = 500
FLUSH_S = 1.0
_buffer = None
_flusher: threading.Thread | None = None
_stop = threading.Event()


def namespace() -> str:
    return (os.environ.get("KH_EVENTS_NS") or config.EVENTS_NS or "kh").strip() or "kh"


def get_buffer():
    global _buffer
    if _buffer is None:
        from app.eventing.queue import make_buffer

        _buffer = make_buffer()
    return _buffer


def set_buffer(buf) -> None:
    """Tests / sidecar: replace the EventBuffer."""
    global _buffer
    _buffer = buf


def flush_once(n: int = BATCH) -> int:
    """Hand one batch to the buffer. Returns rows sent; on failure the batch goes back to the front."""
    batch = drain(n)
    if not batch:
        return 0
    ns = namespace()
    try:
        get_buffer().publish([(ns, r) for r in batch])
    except Exception as e:  # noqa: BLE001
        with _lock:
            _outbox.extendleft(reversed(batch))
            stats["send_errors"] += 1
        log.debug("event flush failed (%d kept): %s", len(batch), e)
        return 0
    with _lock:
        stats["sent"] += len(batch)
    return len(batch)


def flush_all(timeout_s: float = 5.0) -> int:
    end = time.time() + timeout_s
    sent = 0
    while _outbox and time.time() < end:
        k = flush_once()
        if k == 0:
            break
        sent += k
    return sent


def _run() -> None:
    while not _stop.wait(FLUSH_S):
        try:
            while flush_once() >= BATCH:
                pass
        except Exception as e:  # noqa: BLE001
            log.debug("flusher tick failed: %s", e)


def start_flusher() -> bool:
    global _flusher
    if _flusher is not None and _flusher.is_alive():
        return False
    _stop.clear()
    _flusher = threading.Thread(target=_run, name="kh-events-flusher", daemon=True)
    _flusher.start()
    return True


def stop_flusher() -> None:
    global _flusher
    _stop.set()
    if _flusher is not None:
        _flusher.join(timeout=2)
    _flusher = None
    try:
        flush_all(5.0)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# kh.api.call middleware
# ---------------------------------------------------------------------------

_SKIP = ("/health", "/queue/", "/favicon.ico", "/static/")


class ApiCallMiddleware:
    """Pure ASGI middleware: one `kh.api.call {method, path, status, ms, hook_type?}` per request
    (ix_s2 = path, ix_n1 = ms), except paths starting /health, /queue/, /favicon.ico, /static/."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not enabled():
            return await self.app(scope, receive, send)
        path = scope.get("path") or ""
        if path.startswith(_SKIP):
            return await self.app(scope, receive, send)
        t0 = time.perf_counter()
        status = {"code": 500}

        async def _send(msg):
            if msg.get("type") == "http.response.start":
                status["code"] = int(msg.get("status") or 0)
            await send(msg)

        try:
            await self.app(scope, receive, _send)
        finally:
            try:
                ms = round((time.perf_counter() - t0) * 1000, 1)
                hook = None
                for k, v in scope.get("headers") or []:
                    if k == b"x-kh-hook":
                        hook = v.decode("latin-1")[:64]
                body = {"method": scope.get("method"), "path": path, "status": status["code"], "ms": ms}
                if hook:
                    body["hook_type"] = hook
                email = actor = None
                try:
                    from app import auth

                    p = auth.current()
                    email = getattr(p, "email", None) if p is not None else None
                    actor = auth.current_actor()
                except Exception:  # noqa: BLE001
                    pass
                emit("kh.api.call", body, email=email, actor=actor, ix2=path, n1=ms)
            except Exception:  # noqa: BLE001
                pass
