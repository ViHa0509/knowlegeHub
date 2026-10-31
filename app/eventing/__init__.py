"""KH's own events (spec §1.15, §4.9). `emit.emit(...)` is the stable entry point.

`attach(app)` (route group `eventing`): inits the event store, installs `ApiCallMiddleware` (unless app.auth
provides its own, installed innermost by `auth.install`), registers `GET /events`, `GET /events/prompts`, `GET /events:stats`, and an atexit flush.
`start(app)` starts the flusher thread when `KH_EVENTS=on`; `stop()` stops it and drains.
"""
from __future__ import annotations

import atexit
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Query

from app.eventing import emit as _emit

log = logging.getLogger(__name__)

STAT_FIELDS = ("kind", "project", "session_id", "ix_s1", "ix_s2")
PROMPT_KIND = "kh.session.prompt"


def _csv(v: Optional[str]) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _emit_counters() -> dict:
    s = _emit.stats
    return {"emit_emitted": s["emitted"], "emit_sent": s["sent"], "emit_dropped": s["dropped"],
            "emit_send_errors": s["send_errors"]}


def list_events(after_seq: int = 0, kind: Optional[str] = None, session: Optional[str] = None,
                project: Optional[str] = None, limit: int = 100) -> dict:
    from app.eventing.store import get_store

    limit = max(1, min(int(limit or 100), 1000))
    kinds = [k for k in _csv(kind) if k != PROMPT_KIND]
    if _csv(kind) and not kinds:
        events = []
    else:
        events = get_store().read(_emit.namespace(), after_seq, kinds=kinds, hidden=[PROMPT_KIND],
                                  filters={"session_id": session, "project": project}, limit=limit)
    nxt = events[-1]["seq"] if events else int(after_seq or 0)
    return {"events": events, "next_after_seq": nxt, "enabled": _emit.enabled(), "pending": _emit.pending(),
            **_emit_counters()}


def attach(app: FastAPI) -> None:
    from app.eventing.store import get_store

    try:
        get_store().init()
    except Exception as e:  # noqa: BLE001
        log.warning("event store init failed: %s", e)
    try:
        from app import auth as _auth

        auth_has_mw = hasattr(_auth, "ApiCallMiddleware")
    except Exception:  # noqa: BLE001
        auth_has_mw = False
    if not auth_has_mw and not getattr(app.state, "kh_api_call_mw", False):
        app.add_middleware(_emit.ApiCallMiddleware)
        app.state.kh_api_call_mw = True
    atexit.register(_atexit_flush)

    @app.get("/events", tags=["events"])
    def events_route(after_seq: int = 0, kind: Optional[str] = None, session: Optional[str] = None,
                     project: Optional[str] = None, limit: int = Query(100, ge=1, le=1000)):
        return list_events(after_seq, kind, session, project, limit)

    @app.get("/events/prompts", tags=["events"])
    def events_prompts(session: Optional[str] = None, after_seq: int = 0, limit: int = Query(100, ge=1, le=1000)):
        from app import auth

        p = auth.current()
        if auth.enabled() and p is None:
            raise HTTPException(status_code=401, detail="not signed in")
        filters = {"session_id": session}
        if p is not None:
            filters["ix_s1"] = p.email
        rows = get_store().read(_emit.namespace(), after_seq, kinds=[PROMPT_KIND], filters=filters, limit=limit)
        nxt = rows[-1]["seq"] if rows else int(after_seq or 0)
        return {"events": rows, "next_after_seq": nxt, "enabled": _emit.enabled()}

    @app.get("/events:stats", tags=["events"])
    def events_stats(group_by: str = "kind", since: Optional[str] = None):
        cols = _csv(group_by) or ["kind"]
        bad = [c for c in cols if c not in STAT_FIELDS]
        if bad:
            raise HTTPException(status_code=400,
                                detail=f"group_by must be a subset of {', '.join(STAT_FIELDS)}; got {', '.join(bad)}")
        since_ms = None
        if since:
            from app import hotlog

            t = hotlog.parse_ts(since)
            if t == 0.0 and since.strip() not in ("0", "0.0"):
                raise HTTPException(status_code=400, detail=f"cannot parse time '{since}' (ISO-8601 or epoch seconds)")
            since_ms = int(t * 1000)
        return {"groups": get_store().stats(_emit.namespace(), cols, since_ms)}


def _atexit_flush() -> None:
    try:
        _emit.flush_all(5.0)
    except Exception:  # noqa: BLE001
        pass


def start(app: FastAPI) -> None:  # noqa: ARG001
    if _emit.enabled():
        _emit.start_flusher()


def stop() -> None:
    _emit.stop_flusher()
