"""Event buffers (spec §1.15, §4.9): `KH_EVENT_BUFFER` = direct | memory | http.

- direct: append straight to the store (default without `KH_EVENTS_URL`).
- memory: bounded deque (`KH_EVENTS_QUEUE_MAX` 50000) + one writer thread flushing `KH_EVENTS_BATCH_MAX` (500)
  per tick, at least every `KH_EVENTS_FLUSH_S` (1 s); full -> `QueueFull`; a failed namespace batch goes back
  to the front; graceful stop drains up to 8 s. Used inside the sidecar.
- http: POST `{KH_EVENTS_URL}/queue/publish {"events":[{"ns","row"}]}` with bearer `KH_EVENTS_TOKEN`;
  503 -> QueueFull; >= 300 -> error. Default when `KH_EVENTS_URL` is set.
At-least-once delivery; the store dedupes by event id.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Sequence

from app import config

log = logging.getLogger(__name__)


class QueueFull(Exception):
    pass


def _items_by_ns(items: Sequence[tuple[str, dict]]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for ns, row in items:
        out.setdefault(ns, []).append(row)
    return out


class DirectBuffer:
    kind = "direct"

    def __init__(self, store=None):
        from app.eventing.store import get_store

        self.store = store or get_store()

    def publish(self, items: Sequence[tuple[str, dict]]) -> int:
        for ns, rows in _items_by_ns(items).items():
            self.store.append(ns, rows)
        return len(items)

    def stats(self) -> dict:
        return {"kind": self.kind}

    def stop(self) -> None:
        return None


class MemoryQueue:
    kind = "memory"

    def __init__(self, store=None, max_items: int | None = None, batch: int | None = None,
                 flush_s: float | None = None, start: bool = True):
        from app.eventing.store import get_store

        self.store = store or get_store()
        self.max_items = max_items or config._int("KH_EVENTS_QUEUE_MAX", 50000)
        self.batch = batch or config._int("KH_EVENTS_BATCH_MAX", 500)
        self.flush_s = flush_s or config._float("KH_EVENTS_FLUSH_S", 1.0)
        self._q: deque = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.written = 0
        self.errors = 0
        self._thread = None
        if start:
            self._thread = threading.Thread(target=self._run, name="kh-events-writer", daemon=True)
            self._thread.start()

    def depth(self) -> int:
        return len(self._q)

    def publish(self, items: Sequence[tuple[str, dict]]) -> int:
        with self._lock:
            if len(self._q) + len(items) > self.max_items:
                raise QueueFull(f"queue full ({len(self._q)}/{self.max_items})")
            self._q.extend(items)
            if len(self._q) >= self.batch:
                self._wake.set()
        return len(items)

    def flush_once(self) -> int:
        with self._lock:
            take = [self._q.popleft() for _ in range(min(self.batch, len(self._q)))]
        if not take:
            return 0
        done = 0
        failed: list = []
        for ns, rows in _items_by_ns(take).items():
            try:
                self.store.append(ns, rows)
                done += len(rows)
            except Exception as e:  # noqa: BLE001
                self.errors += 1
                log.warning("event write failed (%s rows of %s kept): %s", len(rows), ns, e)
                failed.extend((ns, r) for r in rows)
        if failed:
            with self._lock:
                self._q.extendleft(reversed(failed))
        self.written += done
        return done

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.flush_s)
            self._wake.clear()
            try:
                while self.flush_once() >= self.batch:
                    pass
            except Exception as e:  # noqa: BLE001
                log.warning("event writer tick failed: %s", e)

    def stop(self, drain_s: float = 8.0) -> None:
        self._stop.set()
        self._wake.set()
        end = time.time() + drain_s
        while self._q and time.time() < end:
            if self.flush_once() == 0 and self._q:
                time.sleep(0.05)

    def stats(self) -> dict:
        return {"kind": self.kind, "depth": self.depth(), "max": self.max_items, "written": self.written,
                "errors": self.errors}


class HttpBuffer:
    kind = "http"

    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 10.0, session=None):
        self.url = (url or os.environ.get("KH_EVENTS_URL") or "").rstrip("/")
        self.token = token if token is not None else (os.environ.get("KH_EVENTS_TOKEN") or "")
        self.timeout = timeout
        self._session = session

    def _post(self, payload: dict):
        if self._session is not None:
            return self._session.post(self.url + "/queue/publish", json=payload,
                                      headers={"Authorization": f"Bearer {self.token}"}, timeout=self.timeout)
        import requests

        return requests.post(self.url + "/queue/publish", json=payload,
                             headers={"Authorization": f"Bearer {self.token}"}, timeout=self.timeout)

    def publish(self, items: Sequence[tuple[str, dict]]) -> int:
        payload = {"events": [{"ns": ns, "row": row} for ns, row in items]}
        r = self._post(payload)
        if r.status_code == 503:
            raise QueueFull("sidecar queue full")
        if r.status_code >= 300:
            raise RuntimeError(f"sidecar answered {r.status_code}: {str(getattr(r, 'text', ''))[:200]}")
        return len(items)

    def stats(self) -> dict:
        return {"kind": self.kind, "url": self.url}

    def stop(self) -> None:
        return None


def buffer_kind() -> str:
    v = (os.environ.get("KH_EVENT_BUFFER") or "").strip().lower()
    if v in ("direct", "memory", "http"):
        return v
    return "http" if (os.environ.get("KH_EVENTS_URL") or "").strip() else "direct"


def make_buffer():
    kind = buffer_kind()
    if kind == "http":
        return HttpBuffer()
    if kind == "memory":
        return MemoryQueue()
    return DirectBuffer()
