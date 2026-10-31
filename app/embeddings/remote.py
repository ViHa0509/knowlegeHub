"""Remote embedding adapter and the auto-mode circuit breaker (spec §1.11)."""
from __future__ import annotations

import logging
import random
import threading
import time

import numpy as np

from app import config
from app.embeddings.base import EmbedderMismatch, EmbedderUnavailable, EmbeddingAdapter
from app.embeddings.registry import spec_of

log = logging.getLogger(__name__)

_RETRY_STATUS = {429, 500, 502, 503, 504}


class RemoteEmbeddingAdapter(EmbeddingAdapter):
    def __init__(self, url: str | None = None, token: str | None = None, name: str | None = None):
        self.url = (url or config.EMBED_URL).rstrip("/")
        self.token = config.EMBED_TOKEN if token is None else token
        self.name = name or config.EMBEDDING_MODEL
        s = spec_of(self.name)
        self.dim = int(s.get("dim") or 384)
        self.query_instruction = s.get("query_instruction", "")
        self._tls = threading.local()
        self._ready = False
        self._lock = threading.Lock()

    # -- http -------------------------------------------------------------
    def _session(self):
        import requests  # lazy

        s = getattr(self._tls, "s", None)
        if s is None:
            s = requests.Session()
            if self.token:
                s.headers["Authorization"] = f"Bearer {self.token}"
            self._tls.s = s
        return s

    def _request(self, method: str, path: str, **kw):
        import requests

        last: Exception | None = None
        for attempt in range(config.EMBED_RETRIES + 1):
            try:
                r = self._session().request(method, self.url + path, timeout=config.EMBED_TIMEOUT_S, **kw)
            except (requests.Timeout, requests.ConnectionError) as e:
                last = e
                r = None
            if r is not None:
                if r.status_code < 300:
                    return r
                if r.status_code in (401, 403):
                    raise EmbedderUnavailable(f"embed service refused the token ({r.status_code})")
                if r.status_code not in _RETRY_STATUS:
                    raise ValueError(f"embed service answered {r.status_code}: {r.text[:200]}")
                last = EmbedderUnavailable(f"embed service answered {r.status_code}")
            if attempt >= config.EMBED_RETRIES:
                break
            delay = None
            if r is not None and r.headers.get("Retry-After"):
                try:
                    delay = min(float(r.headers["Retry-After"]), 2.0)
                except ValueError:
                    delay = None
            if delay is None:
                delay = min(0.2 * 2 ** attempt, 2.0) * (0.5 + random.random())
            time.sleep(delay)
        raise EmbedderUnavailable(f"embed service unavailable: {last}")

    # -- contract ---------------------------------------------------------
    def handshake(self) -> None:
        info = self._request("GET", "/embed/info").json()
        if info.get("model") != config.EMBEDDING_MODEL or int(info.get("dim") or 0) != 384:
            raise EmbedderMismatch(
                f"embed service serves {info.get('model')}/{info.get('dim')}, KH expects "
                f"{config.EMBEDDING_MODEL}/384")
        self.dim = int(info["dim"])
        self.query_instruction = info.get("query_instruction") or self.query_instruction
        self._ready = True

    def load(self) -> None:
        try:
            self.handshake()
        except EmbedderMismatch:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("embed service down at startup (%s); will retry before the first vector", e)

    def _ensure(self):
        if not self._ready:
            with self._lock:
                if not self._ready:
                    self.handshake()

    def _encode_batch(self, texts, normalize, kind="passage"):
        self._ensure()
        out: list = []
        step = max(1, config.EMBED_BATCH_MAX)
        for i in range(0, len(texts), step):
            part = texts[i:i + step]
            r = self._request("POST", "/embed", json={"texts": part, "kind": kind, "normalize": bool(normalize)})
            vecs = r.json().get("vectors") or []
            if len(vecs) != len(part):
                raise EmbedderUnavailable("embed service returned a wrong number of vectors")
            out.extend(vecs)
        return np.asarray(out, dtype=np.float32)

    def encode_query(self, text: str) -> list:
        return self.encode([text], normalize_embeddings=True, kind="query")[0].tolist()


class AutoEmbeddingAdapter(EmbeddingAdapter):
    """Circuit breaker: remote first, local fallback (loaded once, lazily)."""

    def __init__(self, remote: RemoteEmbeddingAdapter | None = None, local_factory=None):
        self.remote = remote or RemoteEmbeddingAdapter()
        self.name = self.remote.name
        self.dim = self.remote.dim
        self.query_instruction = self.remote.query_instruction
        self._local_factory = local_factory
        self._local = None
        self._fails = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    def _local_adapter(self):
        with self._lock:
            if self._local is None:
                if self._local_factory is not None:
                    self._local = self._local_factory()
                else:
                    from app.embeddings.registry import make_local

                    self._local = make_local(self.name)
                self._local.load()
            return self._local

    def load(self) -> None:
        self.remote.load()

    def _encode_batch(self, texts, normalize, kind="passage"):
        now = time.monotonic()
        if self._open_until and now < self._open_until:
            return self._local_adapter()._encode_batch(texts, normalize, kind)
        try:
            out = self.remote._encode_batch(texts, normalize, kind)
            self._fails = 0
            self._open_until = 0.0
            return out
        except EmbedderUnavailable as e:
            self._fails += 1
            if self._fails >= config.EMBED_FAIL_THRESHOLD:
                self._open_until = time.monotonic() + config.EMBED_COOLDOWN_S
                log.warning("embed service failing (%s); breaker open for %ss", e, config.EMBED_COOLDOWN_S)
            return self._local_adapter()._encode_batch(texts, normalize, kind)
