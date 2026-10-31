"""Standalone embedding service (spec §1.11): `python -m app.embed_server --host H --port P`.

Routes: GET /health (no token), GET /embed/info, POST /embed. Micro-batching via an asyncio queue.
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
import json
import logging
import sys
import threading

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import config
from app.embeddings import get_embedder

log = logging.getLogger("kh.embed")

_encode_lock = threading.Lock()  # one encoder forward pass at a time


def _token() -> str:
    return config.EMBED_TOKEN


def _authorized(request: Request) -> bool:
    tok = _token()
    if not tok:
        return config.EMBED_ALLOW_NO_TOKEN
    got = request.headers.get("authorization", "")
    return hmac.compare_digest(got.encode(), ("Bearer " + tok).encode())


class Batcher:
    """Collect requests for up to BATCH_WINDOW_MS or MAX_BATCH texts; one pass per normalize flag."""

    def __init__(self, window_ms: float | None = None, max_batch: int | None = None):
        self.window = (config.EMBED_BATCH_WINDOW_MS if window_ms is None else window_ms) / 1000.0
        self.max_batch = max_batch or config.EMBED_MAX_BATCH
        self.queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None

    def start(self):
        self.queue = asyncio.Queue()
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def submit(self, texts: list[str], normalize: bool):
        fut = asyncio.get_running_loop().create_future()
        await self.queue.put((texts, normalize, fut))
        return await fut

    async def _run(self):
        loop = asyncio.get_running_loop()
        while True:
            first = await self.queue.get()
            batch = [first]
            n = len(first[0])
            deadline = loop.time() + self.window
            while n < self.max_batch:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self.queue.get(), remaining)
                except asyncio.TimeoutError:
                    break
                batch.append(item)
                n += len(item[0])
            for flag in (True, False):
                group = [b for b in batch if b[1] is flag]
                if not group:
                    continue
                texts = [t for g in group for t in g[0]]
                try:
                    vecs = await asyncio.to_thread(self._encode, texts, flag)
                    i = 0
                    for g in group:
                        k = len(g[0])
                        if not g[2].done():
                            g[2].set_result(vecs[i:i + k])
                        i += k
                except Exception as e:  # noqa: BLE001
                    for g in group:
                        if not g[2].done():
                            g[2].set_exception(e)

    @staticmethod
    def _encode(texts, normalize):
        with _encode_lock:
            return get_embedder().encode(texts, normalize_embeddings=normalize).tolist()


def create_app() -> FastAPI:
    batcher = Batcher()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app):
        get_embedder().load()
        batcher.start()
        yield
        await batcher.stop()

    app = FastAPI(title="KH embed service", lifespan=lifespan)

    @app.get("/health")
    def health():
        return {"ok": True, "model": config.EMBEDDING_MODEL}

    @app.get("/embed/info")
    def info(request: Request):
        if not _authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        e = get_embedder()
        return {"model": config.EMBEDDING_MODEL, "dim": e.dim, "emb_ver": config.EMB_VER,
                "query_instruction": e.query_instruction, "max_texts": config.EMBED_MAX_TEXTS,
                "max_chars": config.EMBED_MAX_CHARS}

    @app.post("/embed")
    async def embed(request: Request):
        if not _authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > config.EMBED_MAX_BODY:
            return JSONResponse({"error": "too_large"}, status_code=413)
        raw = await request.body()
        if len(raw) > config.EMBED_MAX_BODY:
            return JSONResponse({"error": "too_large"}, status_code=413)
        try:
            body = json.loads(raw)
        except ValueError:
            return JSONResponse({"error": "invalid"}, status_code=400)
        texts = body.get("texts") if isinstance(body, dict) else None
        if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
            return JSONResponse({"error": "invalid", "detail": "texts must be a list of strings"}, status_code=400)
        if len(texts) > config.EMBED_MAX_TEXTS:
            return JSONResponse({"error": "too_many"}, status_code=413)
        kind = body.get("kind", "passage")
        if kind not in ("passage", "query"):
            return JSONResponse({"error": "invalid", "detail": "kind must be passage or query"}, status_code=400)
        normalize = bool(body.get("normalize", True))
        texts = [t[:config.EMBED_MAX_CHARS] for t in texts]
        e = get_embedder()
        if kind == "query" and e.query_instruction:
            texts = [e.query_instruction + t for t in texts]
        vecs = await batcher.submit(texts, normalize) if texts else []
        return {"vectors": vecs, "model": config.EMBEDDING_MODEL, "dim": e.dim}

    return app


def _loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="app.embed_server")
    p.add_argument("--host", default=config.EMBED_HOST)
    p.add_argument("--port", type=int, default=config.EMBED_PORT)
    a = p.parse_args(argv)
    if not _token() and not (config.EMBED_ALLOW_NO_TOKEN and _loopback(a.host)):
        print("refusing to start: set KH_EMBED_TOKEN (or KH_EMBED_ALLOW_NO_TOKEN=true on a loopback host)",
              file=sys.stderr)
        return 2
    if config.EMBED_THREADS > 0:
        try:
            import torch  # noqa: WPS433

            torch.set_num_threads(config.EMBED_THREADS)
        except Exception:
            pass
    import uvicorn

    uvicorn.run(create_app(), host=a.host, port=a.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
