"""Knowledge Hub API (spec §1): app, lifespan, middleware, exception handlers, route groups, core routes."""
from __future__ import annotations

from app import config  # FIRST: loads .env and every tunable

import hashlib
import hmac
import importlib
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_LOG_FMT = "%(asctime)s  %(levelname)-8s  %(name)s - %(message)s"
_handlers: list[logging.Handler] = []
try:
    os.makedirs(config.ROOT / "logs", exist_ok=True)
    _handlers.append(logging.FileHandler(config.ROOT / "logs" / "app.log", encoding="utf-8"))
except OSError:
    pass
if sys.stderr is not None:
    _handlers.append(logging.StreamHandler(sys.stderr))
logging.basicConfig(level=logging.INFO, format=_LOG_FMT, datefmt="%H:%M:%S", handlers=_handlers)
log = logging.getLogger("kh")

HOST, PORT = config.HOST, config.PORT

from app import auth as kh_auth  # noqa: E402
from app import chunks, hotlog, layers, personal, projects  # noqa: E402
from app import vector_store as vs  # noqa: E402
from app.chunker import chunk_with_embeddings, load_model  # noqa: E402
from app.embeddings import EmbedderUnavailable, get_embedder  # noqa: E402
from app.eventing import emit as kh_emit  # noqa: E402
from app.graph_db import get_project_graph, graph_db  # noqa: E402
from app.retriever import clip_hit, query_quick_knowledge  # noqa: E402

# ---------------------------------------------------------------------------
# Route groups (rebuild contract): spec §1.2 step 7 order
# ---------------------------------------------------------------------------

ROUTE_GROUPS = [
    "orchestrator", "projects_routes", "policy", "policy_actions", "routing", "feedback", "messages",
    "artifacts", "gallery", "revisions", "hooks", "personalization", "entities", "turn", "step", "distill",
    "promote", "kernel", "usage", "sync", "home", "learnback", "codegraph", "caps", "session_project",
    "downloads", "eventing", "auth_routes",
]
_CRON_SWITCH = {"feedback": "FEEDBACK_CRON", "messages": "MESSAGE_CRON", "caps": "CAPS_CRONS"}
LOADED_GROUPS: list = []


def _attach_groups(app: FastAPI) -> None:
    for name in ROUTE_GROUPS:
        modname = f"app.{name}"
        try:
            mod = importlib.import_module(modname)
        except ModuleNotFoundError as e:
            if e.name == modname:
                log.warning("route group %s not present; skipped", name)
                continue
            raise
        attach = getattr(mod, "attach", None)
        if not callable(attach):
            log.warning("route group %s has no attach(app); skipped", name)
            continue
        attach(app)
        LOADED_GROUPS.append(mod)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("[1/3] loading embedder (%s)", config.EMBEDDER)
    try:
        app.state.model = load_model()
    except EmbedderUnavailable as e:
        log.error("embedder unavailable at startup: %s", e)
        app.state.model = get_embedder()
    app.state.florence = None
    app.state.florence_processor = None
    try:
        import threading

        app.state.florence_lock = threading.Lock()
    except Exception:  # pragma: no cover
        pass
    try:
        log.info("[2/3] vectors: backend=%s collection=%s docs=%d", config.VECTOR_BACKEND, config.COLLECTION_NAME,
                 vs.shared_collection().count())
    except Exception as e:  # noqa: BLE001
        log.warning("vector count failed: %s", e)
    projects.ensure_orchestrator()
    log.info("[3/3] graph bootstrap")
    try:
        from app.graph_db import backend_name, bootstrap

        bootstrap()
        log.info("graph backend: %s", backend_name())
    except Exception as e:  # noqa: BLE001
        log.error("graph bootstrap failed: %s", e)
        if config.GRAPH_BACKEND == "hana":
            raise
    for mod in LOADED_GROUPS:
        short = mod.__name__.rsplit(".", 1)[-1]
        switch = _CRON_SWITCH.get(short)
        if switch and not getattr(config, switch, True):
            continue
        start = getattr(mod, "start", None)
        if callable(start):
            try:
                start(app)
            except Exception as e:  # noqa: BLE001
                log.error("start() of %s failed: %s", short, e)
    try:
        r = projects.sync_orchestrator_registry()
        log.info("C4 project registry: %s", r)
    except Exception as e:  # noqa: BLE001
        log.warning("C4 registry sync failed (retry: POST /projects/registry/sync): %s", e)
    if kh_auth.enabled() and callable(getattr(kh_auth, "seed_guide", None)):
        try:
            kh_auth.seed_guide(lambda t: get_embedder().encode(t, normalize_embeddings=True))
        except Exception as e:  # noqa: BLE001
            log.warning("auth guide seed failed: %s", e)
    log.info("READY on %s:%s (version %s)", HOST, PORT, config.KH_VERSION)
    yield
    for mod in reversed(LOADED_GROUPS):
        stop = getattr(mod, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception as e:  # noqa: BLE001
                log.warning("stop() of %s failed: %s", mod.__name__, e)
    app.state.model = None
    app.state.florence = None


app = FastAPI(
    title="Knowledge Hub API",
    version=config.KH_VERSION,
    lifespan=lifespan,
    openapi_tags=[
        {"name": "vector", "description": "Vector rows, per-project collections"},
        {"name": "log", "description": "Worklogs and the hot log window"},
        {"name": "keys", "description": "Canonical keys"},
        {"name": "graph", "description": "Graph store"},
        {"name": "layers", "description": "C4 zoom levels"},
        {"name": "system", "description": "Health and version"},
    ],
)

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

kh_auth.install(app)

if config.API_KEY and not config.AUTH_ISSUER:
    _API_KEY = config.API_KEY.encode()

    @app.middleware("http")
    async def api_key_middleware(request: Request, call_next):
        if request.url.path != "/health":
            got = request.headers.get("x-kh-key") or ""
            authz = request.headers.get("authorization") or ""
            if not got and authz.lower().startswith("bearer "):
                got = authz[7:].strip()
            if not got or not hmac.compare_digest(got.encode(), _API_KEY):
                return JSONResponse({"detail": "missing or invalid API key"}, status_code=401)
        return await call_next(request)


# Shared helpers for route groups (avoid importing main from app modules: circular).
app.state.read_lanes = vs.read_lanes
_attach_groups(app)

# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


@app.exception_handler(EmbedderUnavailable)
async def _embedder_unavailable(_request: Request, exc: EmbedderUnavailable):
    return JSONResponse({"error": "embedder_unavailable", "detail": str(exc)}, status_code=503,
                        headers={"Retry-After": "5"})


@app.exception_handler(RequestValidationError)
async def _validation(request: Request, exc: RequestValidationError):
    log.warning("422 %s %s: %s", request.method, request.url.path, exc.errors())
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(ResponseValidationError)
async def _response_validation(_request: Request, exc: ResponseValidationError):
    return JSONResponse({"detail": f"Response model error: {len(exc.errors())} validation error(s)"},
                        status_code=500)


@app.exception_handler(ValidationError)
async def _pyd_validation(_request: Request, exc: ValidationError):
    return JSONResponse({"detail": f"Response model error: {exc.error_count()} validation error(s)"},
                        status_code=500)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _model():
    return getattr(app.state, "model", None) or get_embedder()


def _encode(text):
    return _model().encode(text, normalize_embeddings=True)


def _principal_email() -> str | None:
    p = kh_auth.current()
    return getattr(p, "email", None) if p is not None else None


def _visible(meta: dict | None, rid: str | None) -> bool:
    return kh_auth.row_visible(meta or {}, rid)


def _clip_row(row: dict, max_chars: int | None) -> dict:
    text = row.get("source_text") or ""
    out = {"vector_id": row["vector_id"], "source_text": text, "metadata": row.get("metadata") or {},
           "truncated": False, "text_len": len(text)}
    if max_chars and len(text) > max_chars:
        out["source_text"] = text[:max_chars]
        out["truncated"] = True
    return out


def _parse_bound(v: Optional[str]) -> Optional[float]:
    if v is None or str(v).strip() == "":
        return None
    t = hotlog.parse_ts(v)
    if t == 0.0 and str(v).strip() not in ("0", "0.0"):
        raise HTTPException(status_code=400, detail=f"cannot parse time '{v}' (ISO-8601 or epoch seconds)")
    return t


# ---------------------------------------------------------------------------
# /vector/*
# ---------------------------------------------------------------------------


class VectorAddRequest(BaseModel):
    text: str = Field(..., min_length=1)
    id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    project: Optional[str] = None
    scope: str = "knowledge"


def vector_add(body: VectorAddRequest) -> dict:
    meta_in = dict(body.metadata or {})
    top = (body.project or "").strip()
    if top:
        for key in ("project", "source_id"):
            other = meta_in.get(key)
            if other is not None and str(other).strip():
                if str(other).strip() != top:
                    raise HTTPException(status_code=422,
                                        detail=f"project '{top}' and metadata.{key} '{other}' disagree; give one")
                break
        meta_in["project"] = top
    scope = body.scope
    try:
        vec = _encode(body.text)
    except EmbedderUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Embedding failed: {e}")
    row_id = body.id or "vec_" + hashlib.sha256(body.text.encode()).hexdigest()[:12]
    meta = {"knowledge_type": "quick", "scope": scope, **meta_in}
    project = meta.get("project") or meta.get("source_id")
    project = vs.normalize_project(personal.write_target(scope, project))
    principal = kh_auth.current()
    if kh_auth.enabled() and scope in (config.HOTLOG_SCOPE, "findings") and vs.own_project(project) == vs.ORCHESTRATOR:
        pid = personal.personal_id(principal.email) if principal is not None else "personal-<you>"
        raise HTTPException(status_code=409,
                            detail=("the orchestrator keeps no worklogs or findings; name none and it goes to "
                                    f"your personal project {pid}"))
    if project and personal.is_personal(project):
        owner = principal.email if principal is not None and personal.personal_id(principal.email) == project else None
        try:
            personal.ensure(project, owner)
        except Exception as e:  # noqa: BLE001
            log.warning("personal.ensure(%s) failed: %s", project, e)
    if principal is not None:
        meta["author"] = principal.email
    actor = kh_auth.current_actor()
    if actor:
        meta["actor"] = actor
    else:
        meta.pop("actor", None)
    windowed = hotlog.is_windowed(scope)
    if windowed:
        au = getattr(principal, "acting_user", None) if principal is not None else None
        if au:
            meta["acting_user"] = au
        else:
            meta.pop("acting_user", None)
        owner_project = vs.own_project(project)
        meta["project"] = owner_project
        kh_emit.emit("kh.worklog.stored", {"project": owner_project,
                                           "project_from": meta.get("project_from") or "body"},
                     project=owner_project, email=_principal_email(), actor=actor)
        coll = hotlog.collection() if hotlog.enabled() else vs.get_or_create(
            vs.part_collection_name(scope, owner_project))
        chunk_owner = owner_project
    else:
        if project:
            meta["project"] = project
        coll = vs.scope_collection(scope, project)
        chunk_owner = project if (project and personal.is_personal(project)) else vs.own_project(project)
    try:
        vs.upsert_one(coll, row_id, vec, body.text, meta)
    except Exception as e:  # noqa: BLE001
        log.exception("vector store upsert failed")
        raise HTTPException(status_code=500, detail=f"Vector store error: {e}")
    if scope:
        try:
            chunks.write_for(row_id, body.text, meta, chunk_owner, _model())
        except Exception as e:  # noqa: BLE001
            log.warning("chunk vectors for %s failed: %s", row_id, e)
    if windowed:
        try:
            hotlog.enforce(_principal_email(), (lambda p: kh_auth.may(p, "project.log")) if principal else None)
        except Exception as e:  # noqa: BLE001
            log.warning("hot window enforce failed: %s", e)
    return {"status": "ok", "vector_id": row_id, "dimension": len(vec)}


@app.post("/vector/add", tags=["vector"])
def vector_add_route(body: VectorAddRequest):
    return vector_add(body)


class QuickQueryRequest(BaseModel):
    text: str = Field(..., min_length=1)
    k: int = Field(1, ge=1, le=200)
    scope: Optional[str] = None
    project: Optional[str] = None
    where: Optional[dict[str, Any]] = None
    max_chars: Optional[int] = Field(None, ge=50, le=100000)
    min_score: Optional[float] = Field(None, ge=0, le=1)


@app.post("/vector/query", tags=["vector"])
def vector_query(body: QuickQueryRequest):
    if body.project and not kh_auth.may(vs.normalize_project(body.project), "project.read"):
        raise HTTPException(status_code=403, detail="missing permission project.read on this project")
    hits = query_quick_knowledge(body.text, body.k, _model(), body.scope, body.where, body.project)
    out = []
    for h in hits:
        if not _visible(h.get("metadata"), h["vector_id"]):
            continue
        if body.min_score is not None and h["score"] < body.min_score:
            continue
        clip_hit(h, body.max_chars, body.text)
        out.append({"vector_id": h["vector_id"], "score": h["score"], "source_text": h.get("source_text"),
                    "summary": h.get("summary") or "", "truncated": h.get("truncated", False),
                    "text_len": h.get("text_len", len(h.get("source_text") or "")),
                    "piece_at": h.get("piece_at")})
    return {"hits": out}


class ScrollRequest(BaseModel):
    scope: Optional[str] = None
    project: Optional[str] = None
    where: Optional[dict[str, Any]] = None
    limit: int = Field(500, ge=1, le=5000)
    offset: int = Field(0, ge=0)
    order: Optional[Literal["newest", "oldest"]] = None
    since: Optional[str] = None
    ids: Optional[list[str]] = Field(None, max_length=200)
    max_chars: Optional[int] = Field(None, ge=1)


@app.post("/vector/scroll", tags=["vector"])
def vector_scroll(body: ScrollRequest):
    lanes = vs.read_lanes(body.scope, body.project, body.where)
    if body.ids:
        found: dict[str, dict] = {}
        want = list(dict.fromkeys(body.ids))
        for coll, w in lanes:
            missing = [i for i in want if i not in found]
            if not missing:
                break
            for r in vs.get_ids_vt(coll, missing, w):
                if r["vector_id"] not in found and _visible(r["metadata"], r["vector_id"]):
                    found[r["vector_id"]] = r
        rows = [_clip_row(found[i], body.max_chars) for i in want if i in found]
        return {"matched": len(rows), "count": len(rows), "offset": 0, "limit": body.limit, "hits": rows}
    sorting = bool(body.order or body.since)
    fetch = 1_000_000 if sorting else body.offset + body.limit
    matched = 0
    seen: set = set()
    rows: list[dict] = []
    for coll, w in lanes:
        res = vs.scroll_vt(coll, w, fetch, 0)
        hidden = 0
        for r in res["hits"]:
            if not _visible(r["metadata"], r["vector_id"]):
                hidden += 1
                continue
            if r["vector_id"] in seen:
                continue
            seen.add(r["vector_id"])
            rows.append(r)
        matched += max(0, res["matched"] - hidden)
    if sorting:
        since = _parse_bound(body.since)
        if since is not None:
            rows = [r for r in rows if hotlog.parse_ts(r["metadata"].get("ts")) >= since]
        rows.sort(key=lambda r: hotlog.parse_ts(r["metadata"].get("ts")), reverse=(body.order != "oldest"))
        matched = len(rows)
    page = rows[body.offset:body.offset + body.limit]
    hits = [_clip_row(r, body.max_chars) for r in page]
    return {"matched": matched, "count": len(hits), "offset": body.offset, "limit": body.limit, "hits": hits}


class VectorDeleteRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1, max_length=100)
    scope: Optional[str] = None
    project: Optional[str] = None
    dry_run: bool = False


@app.api_route("/vector/delete", methods=["POST", "DELETE"], tags=["vector"])
def vector_delete(body: VectorDeleteRequest):
    ids = list(dict.fromkeys(body.ids))
    if body.scope:
        targets = vs.read_lanes(body.scope, body.project, None)
    else:
        targets = [(c, None) for c in vs.every_collection()]
    found: dict[str, str] = {}
    searched: list[str] = []
    plan: list[tuple[Any, list[str]]] = []
    for coll, w in targets:
        missing = [i for i in ids if i not in found]
        if not missing:
            break
        searched.append(coll.name)
        got = coll.get(ids=missing, where=w or None, include=[]).get("ids") or []
        for i in got:
            found[i] = coll.name
        if got:
            plan.append((coll, got))
    if not body.dry_run:
        for coll, got in plan:
            coll.delete(ids=got)
        if found:
            try:
                chunks.delete_for(list(found))
            except Exception as e:  # noqa: BLE001
                log.warning("chunk cleanup failed: %s", e)
    return {"status": "ok", "deleted": 0 if body.dry_run else len(found), "found": found,
            "not_found": [i for i in ids if i not in found], "searched": searched, "dry_run": body.dry_run}


class RechunkRequest(BaseModel):
    scope: Optional[str] = None
    project: Optional[str] = None
    dry_run: Any = True


@app.post("/vector/rechunk", tags=["vector"])
def vector_rechunk(body: RechunkRequest):
    if not body.scope:
        raise HTTPException(status_code=400, detail="scope is required")
    dry = body.dry_run is not False
    rows = long_rows = written = 0
    seen: set = set()
    for coll, w in vs.read_lanes(body.scope, body.project, None):
        for r in vs.get_vt(coll, where=w):
            if r["vector_id"] in seen or "#c" in r["vector_id"]:
                continue
            seen.add(r["vector_id"])
            rows += 1
            text = r["source_text"] or ""
            wins = chunks.windows(text)
            if not wins:
                continue
            long_rows += 1
            meta = r["metadata"] or {}
            p = meta.get("project")
            owner = p if (p and personal.is_personal(p)) else vs.own_project(p or body.project)
            if dry:
                written += len(wins)
            else:
                written += chunks.write_for(r["vector_id"], text, meta, owner, _model())
    return {"rows": rows, "long_rows": long_rows, "chunks_written": written, "dry_run": dry}


@app.get("/vector/topics", tags=["vector"])
def vector_topics(field: str = "topic", scope: Optional[str] = None, project: Optional[str] = None):
    counts: dict = {}
    total = missing = 0
    seen: set = set()
    for coll, w in vs.read_lanes(scope, project, None):
        for rid, m in vs.meta_rows_vt(coll, w):
            if rid in seen or not _visible(m, rid):
                continue
            seen.add(rid)
            total += 1
            v = m.get(field)
            if v is None or v == "":
                missing += 1
            else:
                counts[v] = counts.get(v, 0) + 1
    return {"field": field, "distinct": len(counts), "total_scanned": total, "missing_field": missing,
            "topics": vs.topics_sorted(counts)}


class BackfillMetadataRequest(BaseModel):
    scope: Optional[str] = None
    where: Optional[dict[str, Any]] = None
    field: str = "topic"
    apply: bool = False


@app.post("/vector/backfill-metadata", tags=["vector"])
def vector_backfill_metadata(body: BackfillMetadataRequest):
    where = vs.and_where({"scope": {"$eq": body.scope}} if body.scope else None, body.where)
    return vs.backfill_topic_vt(vs.shared_collection(), where, body.field, body.apply)


class ChunkRequest(BaseModel):
    text: str = Field(..., min_length=1)
    c: float = Field(1.0, ge=0)


@app.post("/vector/chunk", tags=["vector"])
def vector_chunk(body: ChunkRequest):
    return chunk_with_embeddings(body.text, body.c, _model())


# ---------------------------------------------------------------------------
# /log/*
# ---------------------------------------------------------------------------


def log_recent(limit: int = 50, project: Optional[str] = None, since: Optional[str] = None,
               until: Optional[str] = None, topic: Optional[str] = None, max_chars: Optional[int] = None) -> dict:
    """GET /log/recent as a function (also exposed as app.state.log_recent for route groups)."""
    scope = config.HOTLOG_SCOPE
    topic_where = {"topic": {"$eq": topic}} if topic else None
    project = vs.normalize_project(project)
    if project:
        lanes = vs.read_lanes(scope, project, topic_where)
    else:
        base = vs.and_where({"scope": {"$eq": scope}}, topic_where)
        lane = hotlog.read_lane(scope, None, hotlog.recent_where(None, _principal_email(), base))
        lanes = [lane] if lane is not None else []
    lo, hi = _parse_bound(since), _parse_bound(until)
    stubs: list[tuple[str, float, int]] = []
    seen: set = set()
    for li, (coll, w) in enumerate(lanes):
        for rid, m in vs.meta_rows_vt(coll, w):
            if rid in seen or not kh_auth.acting_visible(m) or not _visible(m, rid):
                continue
            seen.add(rid)
            ts = hotlog.parse_ts(m.get("ts"))
            if lo is not None and ts < lo:
                continue
            if hi is not None and ts >= hi:
                continue
            stubs.append((rid, ts, li))
    stubs.sort(key=lambda s: s[1], reverse=True)
    matched = len(stubs)
    page = stubs[:max(0, int(limit))]
    by_lane: dict[int, list[str]] = {}
    for rid, _ts, li in page:
        by_lane.setdefault(li, []).append(rid)
    rows: dict[str, dict] = {}
    for li, ids in by_lane.items():
        coll, w = lanes[li]
        for r in vs.get_ids_vt(coll, ids, w):
            rows[r["vector_id"]] = r
    hits = [_clip_row(rows[rid], max_chars) for rid, _ts, _li in page if rid in rows]
    return {"matched": matched, "count": len(hits), "offset": 0, "limit": limit, "hits": hits}


app.state.log_recent = log_recent


@app.get("/log/recent", tags=["log"])
def log_recent_route(limit: int = 50, project: Optional[str] = None, since: Optional[str] = None,
                     until: Optional[str] = None, topic: Optional[str] = None, max_chars: Optional[int] = None):
    return log_recent(limit, project, since, until, topic, max_chars)


@app.get("/log/window", tags=["log"])
def log_window():
    return hotlog.window(_principal_email(), kh_auth.visible_projects("project.read"))


@app.get("/log/by", tags=["log"])
def log_by(author: str, since: Optional[str] = None, until: Optional[str] = None, project: Optional[str] = None,
           limit: int = 500):
    who = {"author": {"$eq": author.strip().lower()}}
    lanes = vs.read_lanes(config.HOTLOG_SCOPE, project, who)
    lo, hi = _parse_bound(since), _parse_bound(until)
    rows, seen = [], set()
    for coll, w in lanes:
        for r in vs.get_vt(coll, where=w):
            if r["vector_id"] in seen:
                continue
            seen.add(r["vector_id"])
            ts = hotlog.parse_ts(r["metadata"].get("ts"))
            if (lo is not None and ts < lo) or (hi is not None and ts >= hi):
                continue
            rows.append((ts, r))
    rows.sort(key=lambda t: t[0], reverse=True)
    hits = [_clip_row(r, None) for _ts, r in rows[:max(0, int(limit))]]
    return {"matched": len(rows), "count": len(hits), "offset": 0, "limit": limit, "hits": hits}


class BackfillAuthorRequest(BaseModel):
    author: str = Field(..., min_length=3)
    projects: list[str] = Field(..., min_length=1)
    apply: bool = False


@app.post("/log/backfill-author", tags=["log"])
def log_backfill_author(body: BackfillAuthorRequest):
    author = body.author.strip().lower()
    out, applied, missing = [], 0, 0
    for p in body.projects:
        pid = vs.own_project(p)
        scanned = miss = 0
        targets = []
        h = vs.get_existing(vs.part_collection_name(config.HOTLOG_SCOPE, pid))
        if h is not None:
            targets.append((h, None))
        if vs.collection_exists(config.HOTLOG_COLLECTION):
            targets.append((hotlog.collection(), {"project": {"$eq": pid}}))
        for coll, w in targets:
            r = vs.fill_missing_field_vt(coll, w, "author", author, body.apply)
            scanned += r["scanned"]
            miss += r["missing"]
            applied += r["applied"]
        missing += miss
        out.append({"project": pid, "scanned": scanned, "missing": miss})
    return {"author": author, "applied": applied, "missing": missing, "projects": out}


# ---------------------------------------------------------------------------
# /keys/*
# ---------------------------------------------------------------------------


class KeysAddRequest(BaseModel):
    key: str = Field(..., min_length=1)
    aliases: list[str] = Field(default_factory=list)
    merge: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    project: Optional[str] = None


class KeysQueryRequest(BaseModel):
    text: str = Field(..., min_length=1)
    k: int = Field(5, ge=1, le=50)
    project: Optional[str] = None


class KeysDeleteRequest(BaseModel):
    keys: list[str] = Field(..., min_length=1)
    project: Optional[str] = None


@app.post("/keys/add", tags=["keys"])
def keys_add(body: KeysAddRequest):
    return vs.keys_add(body.key, body.aliases, body.merge, body.metadata, body.project)


@app.post("/keys/query", tags=["keys"])
def keys_query(body: KeysQueryRequest):
    return {"hits": vs.keys_query(body.text, body.k, body.project)}


@app.post("/keys/delete", tags=["keys"])
def keys_delete(body: KeysDeleteRequest):
    return vs.keys_delete(body.keys, body.project)


# ---------------------------------------------------------------------------
# /graph/*
# ---------------------------------------------------------------------------


class GraphEntity(BaseModel):
    core_id: str = Field(..., min_length=1)
    label: str
    type: Literal["Action", "Artifact", "System", "State", "Actor"]
    attributes: dict[str, Any] = Field(default_factory=dict)
    source_text: str = ""


class GraphEdge(BaseModel):
    source_core_id: str
    relation: str = Field(..., pattern=r"^[A-Z][A-Z0-9_]*$")
    target_core_id: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    source_text: str = ""
    confidence: float = Field(1.0, ge=0, le=1)


class GraphAddRequest(BaseModel):
    entities: list[GraphEntity]
    edges: list[GraphEdge] = Field(default_factory=list)


class GraphQueryRequest(BaseModel):
    core_ids: list[str] = Field(..., min_length=1)


@app.post("/graph/add", tags=["graph"])
def graph_add(body: GraphAddRequest):
    vectors = 0
    if body.entities:
        texts = [e.source_text or e.label or e.core_id for e in body.entities]
        try:
            vecs = _model().encode(texts, normalize_embeddings=True)
        except EmbedderUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"Embedding failed: {e}")
        try:
            vs.upsert_rows(vs.shared_collection(), [e.core_id for e in body.entities], list(vecs), texts,
                           [{"type": e.type} for e in body.entities])
            vectors = len(texts)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"Vector store error: {e}")
    try:
        n = graph_db.upsert_nodes([e.model_dump() for e in body.entities]) if body.entities else 0
        m = graph_db.upsert_edges([e.model_dump() for e in body.edges]) if body.edges else 0
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Graph store error: {e}")
    return {"status": "ok", "entities_inserted": n, "edges_inserted": m, "vectors_generated": vectors}


@app.post("/graph/query", tags=["graph"])
def graph_query(body: GraphQueryRequest):
    nodes, edges = graph_db.fetch_graph(body.core_ids, config.HIERARCHY_EDGES, config.ONTOLOGY_HOPS)
    return {"status": "ok", "nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# /layers/*
# ---------------------------------------------------------------------------


class LayersAddRequest(BaseModel):
    text: str = Field(..., min_length=1)
    kind: Literal["knowledge", "policy"] = "knowledge"
    subject_key: str = ""
    author: str = "agent:kh"
    epoch: int = 1
    ts: str = ""
    auto_layer: Any = None  # ignored (DEAD)
    project: Optional[str] = None


class LayersQueryRequest(BaseModel):
    text: str = Field(..., min_length=1)
    max_level: int = 1
    k: int = 5
    tree_id: str = ""
    kind: str = ""
    project: Optional[str] = None


class LayersGatherRequest(BaseModel):
    subject_key: str = Field(..., min_length=1)
    reconcile: bool = True
    max_level: Optional[int] = None
    project: Optional[str] = None


@app.post("/layers/add", tags=["layers"])
def layers_add(body: LayersAddRequest):
    return layers.add(body.text, body.kind, body.subject_key, body.author, body.epoch, body.ts, body.project,
                      _model())


@app.post("/layers/query", tags=["layers"])
def layers_query(body: LayersQueryRequest):
    return layers.query(body.text, body.max_level, body.k, body.tree_id, body.kind, body.project, _model())


@app.post("/layers/gather", tags=["layers"])
def layers_gather(body: LayersGatherRequest):
    return layers.gather(body.subject_key, body.reconcile, body.max_level, body.project)


# ---------------------------------------------------------------------------
# /vision/locate-ui (minimal: Florence is not part of the rebuild)
# ---------------------------------------------------------------------------


@app.post("/vision/locate-ui", tags=["system"])
async def vision_locate_ui(request: Request):
    try:
        import transformers  # noqa: F401
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="vision model unavailable")
    raise HTTPException(status_code=503, detail="vision model unavailable")


# ---------------------------------------------------------------------------
# DELETE /knowledge
# ---------------------------------------------------------------------------


class KnowledgeDeleteRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1)
    scope: Literal["all", "vector", "graph"] = "all"
    project: Optional[str] = None
    data_scope: str = "knowledge"
    purge_orphan_documents: bool = False


@app.delete("/knowledge", tags=["vector"])
def knowledge_delete(body: KnowledgeDeleteRequest):
    ids = list(dict.fromkeys(body.ids))
    project = vs.normalize_project(body.project)
    vectors_deleted = nodes = edges = docs = 0
    not_found = None
    if body.scope in ("all", "vector"):
        gone: set = set()
        name = vs.scope_collection_name(body.data_scope, project)
        coll = vs.shared_collection() if name is None else vs.get_existing(name)
        if coll is not None:
            gone.update(vs.delete_vt(ids, coll))
        if hotlog.is_windowed(body.data_scope) and vs.collection_exists(config.HOTLOG_COLLECTION):
            hot = hotlog.collection()
            left = [i for i in ids if i not in gone]
            got = hot.get(ids=left, where={"project": {"$eq": vs.own_project(project)}}, include=[]).get("ids") \
                if left else []
            if got:
                hot.delete(ids=got)
                gone.update(got)
        if gone:
            try:
                chunks.delete_for(list(gone))
            except Exception as e:  # noqa: BLE001
                log.warning("chunk cleanup failed: %s", e)
        vectors_deleted = len(gone)
        not_found = [i for i in ids if i not in gone]
    if body.scope in ("all", "graph"):
        g = get_project_graph(project) if project else graph_db
        r = g.delete_nodes(ids)
        nodes, edges = r.get("nodes_deleted", 0), r.get("edges_deleted", 0)
        if body.purge_orphan_documents:
            docs = g.delete_orphan_documents()
    out = {"status": "ok", "requested": len(ids), "scope": body.scope, "vectors_deleted": vectors_deleted,
           "graph_nodes_deleted": nodes, "graph_edges_deleted": edges, "documents_deleted": docs}
    if not_found is not None:
        out["not_found"] = not_found
    return out


@app.delete("/knowledge/orphans", tags=["vector"])
def knowledge_orphans():
    return {"documents_deleted": graph_db.delete_orphan_documents()}


# ---------------------------------------------------------------------------
# /health, /version
# ---------------------------------------------------------------------------


def _model_name() -> str:
    try:
        return get_embedder().name or config.EMBEDDING_MODEL
    except Exception:  # noqa: BLE001
        return config.EMBEDDING_MODEL


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok", "model": _model_name()}


@app.get("/version", tags=["system"])
def version():
    return {"version": config.KH_VERSION, "source": config.KH_VERSION_SOURCE, "model": _model_name()}


if __name__ == "__main__":  # pragma: no cover - desktop/tray mode is not part of the rebuild
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
