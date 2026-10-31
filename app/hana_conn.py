"""HANA connection pools (spec §1.5). `hdbcli` is imported lazily."""
from __future__ import annotations

import logging
import queue
import threading
import time

from app import config

log = logging.getLogger(__name__)


def connect_kwargs() -> dict:
    kw = {
        "address": config.HANA_HOST,
        "port": int(config.HANA_PORT),
        "user": config.HANA_USERNAME,
        "password": config.HANA_PASSWORD,
        "encrypt": bool(config.HANA_ENCRYPT),
        "autocommit": False,
    }
    if config.HANA_ENCRYPT:
        kw["sslValidateCertificate"] = bool(config.HANA_SSL_VALIDATE)
    if config.HANA_CONNECT_TIMEOUT_MS > 0:
        kw["connectTimeout"] = int(config.HANA_CONNECT_TIMEOUT_MS)
    return kw


def hdbcli_available() -> bool:
    try:
        import hdbcli  # noqa: F401
        return True
    except Exception:
        return False


class _Pool:
    def __init__(self, size: int, timeout: float, overflow: int = 0, recycle: int = 0,
                 set_schema: bool = True):
        self.size = max(1, int(size))
        self.timeout = float(timeout)
        self.cap = self.size + max(0, int(overflow))
        self.recycle = int(recycle)
        self.set_schema = set_schema
        self._idle: queue.Queue = queue.Queue(maxsize=self.size)
        self._created = 0
        self._born: dict[int, float] = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

    def _new(self):
        from hdbcli import dbapi  # lazy

        conn = dbapi.connect(**connect_kwargs())
        if self.set_schema:
            cur = conn.cursor()
            try:
                cur.execute(f'SET SCHEMA "{config.HANA_SCHEMA}"')
            finally:
                cur.close()
        self._born[id(conn)] = time.monotonic()
        return conn

    def _usable(self, conn) -> bool:
        try:
            if not conn.isconnected():
                return False
        except Exception:
            return False
        if self.recycle > 0 and time.monotonic() - self._born.get(id(conn), 0) > self.recycle:
            return False
        return True

    def _discard(self, conn) -> None:
        try:
            conn.close()
        except Exception:
            pass
        with self._lock:
            self._created = max(0, self._created - 1)
            self._born.pop(id(conn), None)
            self._cond.notify()

    def acquire(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                conn = None
            if conn is not None:
                if self._usable(conn):
                    return conn
                self._discard(conn)
                continue
            with self._lock:
                if self._created < self.cap:
                    self._created += 1
                    make = True
                else:
                    make = False
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Timed out waiting for a HANA connection")
                    self._cond.wait(min(remaining, 0.05))
            if make:
                try:
                    return self._new()
                except Exception:
                    with self._lock:
                        self._created -= 1
                    raise

    def release(self, conn) -> None:
        try:
            self._idle.put_nowait(conn)
            with self._lock:
                self._cond.notify()
        except queue.Full:
            self._discard(conn)


_store_pool: _Pool | None = None
_store_lock = threading.Lock()


def store_pool() -> _Pool:
    global _store_pool
    if _store_pool is None:
        with _store_lock:
            if _store_pool is None:
                _store_pool = _Pool(
                    size=max(1, config.HANA_POOL_SIZE),
                    timeout=config.HANA_POOL_TIMEOUT,
                    overflow=config.HANA_MAX_OVERFLOW,
                    recycle=config.HANA_POOL_RECYCLE,
                    set_schema=True,
                )
    return _store_pool
