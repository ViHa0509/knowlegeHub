"""kh-events sidecar (spec §1.15, §4.9): `python -m app.eventing.sidecar`.

Routes: `POST /queue/publish {events:[{ns, row}]}` (bearer `KH_EVENTS_TOKEN`; 1..2000 events each with `ns`,
`row.id`, `row.kind`, else 400; full -> 503 + `Retry-After: 5`; ok -> 202 `{accepted, depth}`), `GET /queue/stats`,
`GET /health {status, depth}`. Token from env or a user-provided service in `VCAP_SERVICES`; refuses to start
without one. A MemoryQueue writes to the same relational store (`KH_EVENTS_QUEUE_MAX` 50000,
`KH_EVENTS_BATCH_MAX` 500, `KH_EVENTS_FLUSH_S` 1).
"""
from __future__ import annotations

import hmac
import json
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import config
from app.eventing.queue import MemoryQueue, QueueFull

log = logging.getLogger("kh.events.sidecar")

MAX_EVENTS = 2000


def token_from_env() -> str:
    t = (os.environ.get("KH_EVENTS_TOKEN") or "").strip()
    if t:
        return t
    raw = os.environ.get("VCAP_SERVICES")
    if raw:
        try:
            vcap = json.loads(raw)
        except ValueError:
            vcap = {}
        for entries in (vcap.get("user-provided") or []):
            cred = (entries or {}).get("credentials") or {}
            if cred.get("KH_EVENTS_TOKEN"):
                return str(cred["KH_EVENTS_TOKEN"]).strip()
    return ""


def create_app(token: str | None = None, queue: MemoryQueue | None = None) -> FastAPI:
    tok = token if token is not None else token_from_env()
    if not tok:
        raise SystemExit("kh-events sidecar: KH_EVENTS_TOKEN is required (env or user-provided service)")
    q = queue or MemoryQueue()
    app = FastAPI(title="kh-events")
    app.state.queue = q

    @app.post("/queue/publish")
    async def publish(request: Request):
        auth = request.headers.get("authorization") or ""
        got = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not got or not hmac.compare_digest(got.encode(), tok.encode()):
            return JSONResponse({"detail": "missing or invalid token"}, status_code=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"detail": "body must be JSON"}, status_code=400)
        events = body.get("events") if isinstance(body, dict) else None
        if not isinstance(events, list) or not (1 <= len(events) <= MAX_EVENTS):
            return JSONResponse({"detail": f"events must be a list of 1..{MAX_EVENTS}"}, status_code=400)
        items = []
        for e in events:
            row = (e or {}).get("row") if isinstance(e, dict) else None
            if not isinstance(e, dict) or not e.get("ns") or not isinstance(row, dict) or not row.get("id") \
                    or not row.get("kind"):
                return JSONResponse({"detail": "each event needs ns, row.id and row.kind"}, status_code=400)
            items.append((str(e["ns"]), row))
        try:
            q.publish(items)
        except QueueFull as e:
            return JSONResponse({"detail": str(e)}, status_code=503, headers={"Retry-After": "5"})
        return JSONResponse({"accepted": len(items), "depth": q.depth()}, status_code=202)

    @app.get("/queue/stats")
    def stats():
        return q.stats()

    @app.get("/health")
    def health():
        return {"status": "ok", "depth": q.depth()}

    @app.on_event("shutdown")
    def _drain():
        q.stop()

    return app


def main() -> None:  # pragma: no cover - process entry
    import uvicorn

    logging.basicConfig(level=(os.environ.get("KH_EVENTS_LOG") or "INFO").upper())
    port = config._int("PORT", config._int("KH_EVENTS_PORT", 8090))
    uvicorn.run(create_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
