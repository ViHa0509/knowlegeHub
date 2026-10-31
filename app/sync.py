"""KH-to-KH sync (spec §4.11): copies projects between KHs in code (no LLM). Protocol 2.

Trust: a connection key made by the giver, `khsync1.<base64url {"u": giver URL, "k": kid}>.<secret>`; the giver
stores only sha256(secret) and can revoke. Peer calls carry it in `X-KH-Sync-Key`.

Merge, never delete: id missing -> created; same text+meta -> unchanged; receiver ts newer -> kept; else updated.
Key = reader role: a non-maintainer who pulls with a verified key gets `project.read` on the pulled projects via
`sync_grants`; `key_reads(email)` (cached 30 s) is what `auth.with_key_reads` reads.

Test seam: `PEER_SESSION` (a requests-like session, e.g. a TestClient) replaces HTTP to the giver.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import config, sqldb
from app import vector_store as vs

log = logging.getLogger(__name__)

PROTOCOL = 2
PREFIX = "khsync1"
PROBE = "Knowledge Hub sync probe: the quick brown fox jumps over the lazy dog."
PROBE_MIN = 0.999
PAGE_DEFAULT, PAGE_MAX = 200, 500
DEFAULT_PARTS = ("knowledge", "log", "findings", "entity", "chunk", "personalization")
OPTIONAL_PARTS = ("policy", "src")
GRANT_CACHE_S = 30
GIVEN_STALE_S = 300
PEER_SESSION: Any = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

_ready: set = set()


def _m1(c):
    sqldb.create_table(
        c, "sync_keys",
        "CREATE TABLE IF NOT EXISTS sync_keys (kid TEXT PRIMARY KEY, secret_hash TEXT, label TEXT, created_by TEXT, "
        "created_at TEXT, expires_at TEXT, revoked_at TEXT, last_used_at TEXT, projects TEXT, public_url TEXT)",
        "CREATE COLUMN TABLE sync_keys (kid NVARCHAR(64) PRIMARY KEY, secret_hash NVARCHAR(128), label NVARCHAR(256), "
        "created_by NVARCHAR(256), created_at NVARCHAR(40), expires_at NVARCHAR(40), revoked_at NVARCHAR(40), "
        "last_used_at NVARCHAR(40), projects NCLOB, public_url NVARCHAR(1024))")
    sqldb.create_table(
        c, "sync_jobs",
        "CREATE TABLE IF NOT EXISTS sync_jobs (id TEXT PRIMARY KEY, source TEXT, state TEXT, created_by TEXT, "
        "started TEXT, finished TEXT, doc TEXT)",
        "CREATE COLUMN TABLE sync_jobs (id NVARCHAR(64) PRIMARY KEY, source NVARCHAR(1024), state NVARCHAR(32), "
        "created_by NVARCHAR(256), started NVARCHAR(40), finished NVARCHAR(40), doc NCLOB)")
    sqldb.create_table(
        c, "sync_accounts",
        "CREATE TABLE IF NOT EXISTS sync_accounts (email TEXT PRIMARY KEY, password TEXT, created_at TEXT, job_id TEXT, "
        "source TEXT, projects TEXT, applied TEXT, note TEXT, sent_at TEXT)",
        "CREATE COLUMN TABLE sync_accounts (email NVARCHAR(256) PRIMARY KEY, password NVARCHAR(256), "
        "created_at NVARCHAR(40), job_id NVARCHAR(64), source NVARCHAR(1024), projects NCLOB, applied NCLOB, "
        "note NVARCHAR(1000), sent_at NVARCHAR(40))")
    sqldb.create_table(
        c, "sync_grants",
        "CREATE TABLE IF NOT EXISTS sync_grants (email TEXT NOT NULL, project TEXT NOT NULL, source TEXT, kid TEXT, "
        "job_id TEXT, granted_at TEXT, PRIMARY KEY (email, project))",
        "CREATE COLUMN TABLE sync_grants (email NVARCHAR(256) NOT NULL, project NVARCHAR(256) NOT NULL, "
        "source NVARCHAR(1024), kid NVARCHAR(64), job_id NVARCHAR(64), granted_at NVARCHAR(40), "
        "PRIMARY KEY (email, project))")
    sqldb.create_table(
        c, "sync_given",
        "CREATE TABLE IF NOT EXISTS sync_given (job_id TEXT PRIMARY KEY, kid TEXT, state TEXT, started TEXT, "
        "updated TEXT, doc TEXT)",
        "CREATE COLUMN TABLE sync_given (job_id NVARCHAR(64) PRIMARY KEY, kid NVARCHAR(64), state NVARCHAR(32), "
        "started NVARCHAR(40), updated NVARCHAR(40), doc NCLOB)")


def _m2(c):
    sqldb.add_column(c, "sync_keys", "scope", "TEXT", "NCLOB")


def _conn():
    c = sqldb.connect()
    key = sqldb._location(c)
    if key not in _ready:
        sqldb.migrate(c, "sync", [(1, _m1), (2, _m2)])
        c.execute("UPDATE sync_jobs SET state='interrupted' WHERE state IN ('running', 'queued')")
        c.commit()
        _ready.add(key)
    return c


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def _b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).decode().rstrip("=")


def _unb64(s: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode())


def parse_key(key: str) -> tuple[str, str, str]:
    """-> (giver url, kid, secret); ValueError when malformed."""
    parts = (key or "").strip().split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise ValueError("not a khsync1 key")
    head = _unb64(parts[1])
    if not head.get("u") or not head.get("k"):
        raise ValueError("key head lacks u/k")
    return str(head["u"]).rstrip("/"), str(head["k"]), parts[2]


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def make_key(label: str, days: int, projects: list[str] | None, public_url: str, by: str,
             scope: dict | None = None) -> dict:
    days = max(1, min(int(days or 7), 90))
    kid = "sk_" + uuid.uuid4().hex[:12]
    secret = secrets.token_urlsafe(32)
    key = f"{PREFIX}.{_b64({'u': public_url.rstrip('/'), 'k': kid})}.{secret}"
    exp = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds").replace("+00:00", "Z")
    projects = [p for p in (projects or []) if not vs.is_personal(p)]
    with _conn() as c:
        sqldb.insert_new(c, "sync_keys", {"kid": kid, "secret_hash": _hash(secret), "label": label or "",
                                          "created_by": by, "created_at": _now(), "expires_at": exp,
                                          "revoked_at": None, "last_used_at": None,
                                          "projects": json.dumps(projects), "public_url": public_url,
                                          "scope": json.dumps(scope or {})})
    return {"kid": kid, "key": key, "label": label or "", "expires_at": exp, "projects": projects,
            "public_url": public_url}


def _key_row(kid: str):
    with _conn() as c:
        return c.execute("SELECT * FROM sync_keys WHERE kid=?", (kid,)).fetchone()


def list_keys() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM sync_keys ORDER BY created_at DESC").fetchall()
    return [{"kid": r["kid"], "label": r["label"], "created_by": r["created_by"], "created_at": r["created_at"],
             "expires_at": r["expires_at"], "revoked_at": r["revoked_at"], "last_used_at": r["last_used_at"],
             "projects": json.loads(r["projects"] or "[]"), "public_url": r["public_url"],
             "scope": json.loads(r["scope"] or "{}")} for r in rows]


def revoke_key(kid: str) -> bool:
    with _conn() as c:
        cur = c.execute("UPDATE sync_keys SET revoked_at=? WHERE kid=? AND revoked_at IS NULL", (_now(), kid))
        return bool(cur.rowcount and cur.rowcount > 0)


def check_key(key: str | None) -> dict:
    """Giver side: verify a presented key. Returns the key row as a dict, or raises 401."""
    try:
        _u, kid, secret = parse_key(key or "")
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="invalid sync key")
    r = _key_row(kid)
    if r is None or not hmac.compare_digest(r["secret_hash"] or "", _hash(secret)):
        raise HTTPException(status_code=401, detail="invalid sync key")
    if r["revoked_at"]:
        raise HTTPException(status_code=401, detail="sync key revoked")
    if (r["expires_at"] or "") < _now():
        raise HTTPException(status_code=401, detail="sync key expired")
    with _conn() as c:
        c.execute("UPDATE sync_keys SET last_used_at=? WHERE kid=?", (_now(), kid))
    return {"kid": kid, "projects": json.loads(r["projects"] or "[]"), "scope": json.loads(r["scope"] or "{}")}


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------


def part_collections(project: str, part: str) -> list[tuple[Any, dict | None]]:
    """[(handle, where)] holding a project's part on this KH (log includes the hot window)."""
    from app import chunks

    if part == "chunk":
        names = [chunks.collection_name(project)]
    elif part == "src":
        names = [f"kh_src_{project}"]
    else:
        names = [vs.part_collection_name(part, project)]
    out = []
    for n in names:
        h = vs.get_existing(n)
        if h is not None:
            out.append((h, None))
    if part == config.HOTLOG_SCOPE and vs.collection_exists(config.HOTLOG_COLLECTION):
        out.append((vs.get_existing(config.HOTLOG_COLLECTION), {"project": {"$eq": project}}))
    return out


def part_count(project: str, part: str) -> int:
    n = 0
    for h, where in part_collections(project, part):
        n += len(h.get(where=where, include=[]).get("ids") or []) if where else int(h.count())
    return n


def local_projects() -> list[dict]:
    from app import projects

    out = []
    for pid, e in sorted(projects.list_projects().items()):
        if vs.is_personal(pid):
            continue
        out.append({"id": pid, "descriptor": e.get("descriptor") or "", "aliases": e.get("aliases") or [],
                    "keywords": e.get("keywords") or [], "tools": e.get("tools") or [],
                    "conflicts_with": e.get("conflicts_with") or [],
                    "parts": {p: part_count(pid, p) for p in DEFAULT_PARTS + OPTIONAL_PARTS}})
    return out


def _row_time(meta: dict) -> float | None:
    from app import hotlog

    for k in ("ts", "date", "created_at", "updated_at"):
        v = meta.get(k)
        if v not in (None, ""):
            t = hotlog.parse_ts(v)
            if t:
                return t
    return None


def export_rows(project: str, part: str, offset: int = 0, limit: int = PAGE_DEFAULT, since: str | None = None,
                until: str | None = None, include_undated: bool = True) -> dict:
    from app import hotlog

    limit = max(1, min(int(limit or PAGE_DEFAULT), PAGE_MAX))
    lo = hotlog.parse_ts(since) if since else None
    hi = hotlog.parse_ts(until) if until else None
    rows = []
    for h, where in part_collections(project, part):
        res = h.get(where=where, include=["embeddings", "documents", "metadatas"])
        for i, rid in enumerate(res.get("ids") or []):
            meta = (res.get("metadatas") or [{}])[i] or {}
            t = _row_time(meta)
            if t is None and not include_undated and (lo or hi):
                continue
            if t is not None and ((lo and t < lo) or (hi and t >= hi)):
                continue
            emb = (res.get("embeddings") or [None])[i]
            rows.append({"id": rid, "text": (res.get("documents") or [None])[i], "metadata": meta,
                         "embedding": list(emb) if emb is not None else None})
    rows.sort(key=lambda r: r["id"])
    page = rows[offset:offset + limit]
    nxt = offset + limit if offset + limit < len(rows) else None
    return {"project": project, "part": part, "total": len(rows), "offset": offset, "rows": page, "next_offset": nxt}


# ---------------------------------------------------------------------------
# Receiver side: merge
# ---------------------------------------------------------------------------


def _same(a: dict, b: dict) -> bool:
    skip = {"emb_model", "emb_ver", "emb_dim", "source_text"}
    return {k: v for k, v in (a or {}).items() if k not in skip} == {k: v for k, v in (b or {}).items() if k not in skip}


def merge_rows(project: str, part: str, rows: list[dict], dry: bool = False) -> dict:
    counts = {"created": 0, "unchanged": 0, "kept": 0, "updated": 0}
    if not rows:
        return counts
    from app import chunks

    hot = vs.get_existing(config.HOTLOG_COLLECTION) if part == config.HOTLOG_SCOPE else None
    if part == "chunk":
        target_name = chunks.collection_name(project)
    elif part == "src":
        target_name = f"kh_src_{project}"
    else:
        target_name = vs.part_collection_name(part, project)
    target = vs.get_existing(target_name) if dry else vs.get_or_create(target_name)
    ids = [r["id"] for r in rows]
    existing: dict[str, tuple[Any, dict]] = {}
    for h in [x for x in (hot, target) if x is not None]:
        res = h.get(ids=ids, include=["documents", "metadatas"])
        for i, rid in enumerate(res.get("ids") or []):
            if rid not in existing:
                existing[rid] = (h, {"text": (res.get("documents") or [None])[i],
                                     "meta": (res.get("metadatas") or [{}])[i] or {}})
    for r in rows:
        meta = dict(r.get("metadata") or {})
        cur = existing.get(r["id"])
        if cur is None:
            counts["created"] += 1
            dest = target
        else:
            h, old = cur
            if old["text"] == r.get("text") and _same(old["meta"], meta):
                counts["unchanged"] += 1
                continue
            t_old, t_new = _row_time(old["meta"]), _row_time(meta)
            if t_old is not None and t_new is not None and t_old > t_new:
                counts["kept"] += 1
                continue
            counts["updated"] += 1
            dest = h
        if dry or dest is None:
            continue
        emb = r.get("embedding")
        if emb is None:
            from app.embeddings import get_embedder

            emb = get_embedder().encode(r.get("text") or "", normalize_embeddings=True)
        dest.upsert(ids=[r["id"]], embeddings=[list(map(float, emb))], documents=[r.get("text")], metadatas=[meta])
    return counts


def _register(entry: dict, puller: str | None) -> str:
    from app import projects

    pid = entry["id"]
    old = projects.get_project(pid)
    projects.get_or_create_project(pid, aliases=entry.get("aliases") or [], keywords=entry.get("keywords") or [],
                                   tools=entry.get("tools") or [], conflicts_with=entry.get("conflicts_with") or [],
                                   descriptor=(entry.get("descriptor") or "") if not (old or {}).get("descriptor") else "",
                                   source="sync")
    return "created" if old is None else "merged"


# ---------------------------------------------------------------------------
# Peer HTTP
# ---------------------------------------------------------------------------


def _peer(method: str, url: str, path: str, key: str, body: dict | None = None, params: dict | None = None):
    headers = {"X-KH-Sync-Key": key}
    if PEER_SESSION is not None:
        r = PEER_SESSION.request(method, url + path, json=body, params=params, headers=headers)
    else:
        import requests

        r = requests.request(method, url + path, json=body, params=params, headers=headers, timeout=60)
    try:
        data = r.json()
    except ValueError:
        data = {"text": getattr(r, "text", "")}
    if r.status_code >= 300:
        raise HTTPException(status_code=502 if r.status_code >= 500 else r.status_code,
                            detail=f"giver answered {r.status_code}: {data.get('detail') if isinstance(data, dict) else data}")
    return data


def _model_name() -> str:
    from app.embeddings import get_embedder

    m = get_embedder()
    n = getattr(m, "name", None)
    return n() if callable(n) else (n or config.EMBEDDING_MODEL)


def hello() -> dict:
    from app.embeddings import get_embedder

    v = get_embedder().encode(PROBE, normalize_embeddings=True)
    v = v.tolist() if hasattr(v, "tolist") else list(v)
    return {"protocol": PROTOCOL, "model": _model_name(), "dim": len(v), "probe": v, "kh_version": config.KH_VERSION}


def connect(key: str) -> dict:
    try:
        url, kid, _s = parse_key(key)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="not a valid sync key")
    h = _peer("GET", url, "/sync/peer/hello", key)
    mine = hello()
    cos = float(np.dot(np.asarray(h.get("probe") or [0.0] * mine["dim"], dtype=float),
                       np.asarray(mine["probe"], dtype=float))) if len(h.get("probe") or []) == mine["dim"] else 0.0
    if h.get("protocol") != PROTOCOL or h.get("model") != mine["model"] or h.get("dim") != mine["dim"] or cos < PROBE_MIN:
        raise HTTPException(status_code=409, detail={
            "error": "incompatible", "giver": {k: h.get(k) for k in ("protocol", "model", "dim")},
            "receiver": {k: mine[k] for k in ("protocol", "model", "dim")}, "probe_cosine": round(cos, 6)})
    manifest = _peer("GET", url, "/sync/peer/manifest", key)
    return {"giver": url, "kid": kid, "protocol": PROTOCOL, "model": mine["model"], "probe_cosine": round(cos, 6),
            "manifest": manifest}


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

_cancel: set = set()


def _save_job(job_id: str, state: str, doc: dict, finished: bool = False) -> None:
    with _conn() as c:
        c.execute("UPDATE sync_jobs SET state=?, doc=?, finished=? WHERE id=?",
                  (state, json.dumps(doc, default=str), _now() if finished else None, job_id))


def get_job(job_id: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM sync_jobs WHERE id=?", (job_id,)).fetchone()
    if r is None:
        return None
    return {"id": r["id"], "source": r["source"], "state": r["state"], "created_by": r["created_by"],
            "started": r["started"], "finished": r["finished"], "doc": json.loads(r["doc"] or "{}")}


def list_jobs() -> list[dict]:
    with _conn() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM sync_jobs ORDER BY started DESC").fetchall()]
    return [get_job(i) for i in ids]


def _is_maintainer() -> bool:
    from app import auth

    p = auth.current()
    return p is None or bool(getattr(p, "is_maintainer", False))


def _email() -> str | None:
    from app import auth

    p = auth.current()
    return getattr(p, "email", None) if p is not None else None


def run_pull(job_id: str, key: str, selection: list[dict], since=None, until=None, include_undated=True,
             dry_run=False, email=None, maintainer=True) -> dict:
    url, kid, _s = parse_key(key)
    doc: dict = {"projects": {}, "dry_run": dry_run, "since": since, "until": until}
    last_progress = 0.0

    def progress(state):
        nonlocal last_progress
        if state == "running" and time.time() - last_progress < 2:
            return
        last_progress = time.time()
        try:
            _peer("POST", url, "/sync/peer/progress", key, {"job_id": job_id, "state": state, "doc": doc})
        except Exception as e:  # noqa: BLE001
            log.debug("progress to giver failed: %s", e)

    _save_job(job_id, "running", doc)
    progress("running")
    state = "done"
    try:
        manifest = _peer("GET", url, "/sync/peer/manifest", key)
        entries = {p["id"]: p for p in manifest.get("projects") or []}
        if not maintainer:
            _peer("POST", url, "/sync/peer/verify", key, {})
        for sel in selection:
            pid = sel.get("project")
            if pid not in entries or vs.is_personal(pid):
                doc["projects"][pid] = {"error": "not offered by the giver"}
                continue
            pdoc = {"registry": None, "parts": {}}
            if not dry_run and maintainer:
                pdoc["registry"] = _register(entries[pid], email)
            elif not dry_run:
                from app import projects

                if not projects.exists(pid):
                    projects.get_or_create_project(pid, descriptor=entries[pid].get("descriptor") or "", source="sync")
                    pdoc["registry"] = "created"
            parts = sel.get("parts") or list(DEFAULT_PARTS)
            for part in parts:
                tot = {"created": 0, "unchanged": 0, "kept": 0, "updated": 0, "pages": 0}
                offset = 0
                while offset is not None:
                    if job_id in _cancel:
                        raise _Cancelled()
                    page = _peer("POST", url, "/sync/peer/export", key,
                                 {"project": pid, "part": part, "offset": offset, "limit": PAGE_DEFAULT,
                                  "since": since, "until": until, "include_undated": include_undated})
                    for k, v in merge_rows(pid, part, page.get("rows") or [], dry_run).items():
                        tot[k] += v
                    tot["pages"] += 1
                    offset = page.get("next_offset")
                    doc["projects"].setdefault(pid, pdoc)["parts"][part] = tot
                    _save_job(job_id, "running", doc)
                    progress("running")
                pdoc["parts"][part] = tot
            doc["projects"][pid] = pdoc
            if not maintainer and email and not dry_run:
                with _conn() as c:
                    sqldb.upsert(c, "sync_grants", ["email", "project"],
                                 {"email": email, "project": pid, "source": url, "kid": kid, "job_id": job_id,
                                  "granted_at": _now()})
                _grant_cache.pop(email, None)
    except _Cancelled:
        state = "cancelled"
    except Exception as e:  # noqa: BLE001
        state = "failed"
        doc["error"] = str(getattr(e, "detail", None) or e)[:500]
    _save_job(job_id, state, doc, finished=True)
    _cancel.discard(job_id)
    progress(state)
    return get_job(job_id)


class _Cancelled(Exception):
    pass


def start_pull(key: str, selection: list[dict], since=None, until=None, include_undated=True, dry_run=False,
               wait: bool = False) -> dict:
    try:
        url, _kid, _s = parse_key(key)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="not a valid sync key")
    job_id = "sj_" + uuid.uuid4().hex[:12]
    email, maint = _email(), _is_maintainer()
    with _conn() as c:
        sqldb.insert_new(c, "sync_jobs", {"id": job_id, "source": url, "state": "queued", "created_by": email or "local",
                                          "started": _now(), "finished": None,
                                          "doc": json.dumps({"selection": selection})})
    args = (job_id, key, selection, since, until, include_undated, dry_run, email, maint)
    if wait:
        run_pull(*args)
    else:
        threading.Thread(target=run_pull, args=args, daemon=True, name=f"sync-{job_id}").start()
    return {"job_id": job_id, "state": "queued" if not wait else get_job(job_id)["state"]}


# ---------------------------------------------------------------------------
# Grants (read by app.auth.with_key_reads)
# ---------------------------------------------------------------------------

_grant_cache: dict[str, tuple[float, list[str]]] = {}


def key_reads(email: str) -> list[str]:
    """Projects `email` may read because they pulled them with a verified sync key (cached 30 s)."""
    if not email:
        return []
    hit = _grant_cache.get(email)
    if hit and time.time() - hit[0] < GRANT_CACHE_S:
        return hit[1]
    with _conn() as c:
        pids = [r[0] for r in c.execute("SELECT project FROM sync_grants WHERE email=?", (email,)).fetchall()]
    _grant_cache[email] = (time.time(), pids)
    return pids


def list_grants() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM sync_grants ORDER BY email, project").fetchall()
    return [{k: r[k] for k in ("email", "project", "source", "kid", "job_id", "granted_at")} for r in rows]


def remove_grant(email: str, project: str | None = None) -> int:
    with _conn() as c:
        if project:
            cur = c.execute("DELETE FROM sync_grants WHERE email=? AND project=?", (email, project))
        else:
            cur = c.execute("DELETE FROM sync_grants WHERE email=?", (email,))
    _grant_cache.pop(email, None)
    return int(cur.rowcount or 0)


# ---------------------------------------------------------------------------
# Identity service (used by /home members)
# ---------------------------------------------------------------------------


def _identity(method: str, path: str, body: dict | None = None):
    """Forward to the identity service with the maintainer's own token (app.auth.relay)."""
    from app import auth

    if not auth.enabled():
        raise HTTPException(status_code=409, detail="sign-in is off; there is no identity service")
    relay = getattr(auth, "relay", None)
    if relay is None:
        raise HTTPException(status_code=501, detail="identity relay unavailable")
    status, data = relay(method, path, body)
    if status >= 300:
        raise HTTPException(status_code=status, detail=data)
    return data


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class KeyRequest(BaseModel):
    label: str = ""
    days: int = 7
    projects: Optional[list[str]] = None
    public_url: Optional[str] = None
    parts: Optional[list[str]] = None
    window: Optional[dict] = None
    members: Optional[bool] = None


class ConnectRequest(BaseModel):
    key: str


class PullRequest(BaseModel):
    key: str
    selection: list[dict] = Field(default_factory=list)
    since: Optional[str] = None
    until: Optional[str] = None
    include_undated: bool = True
    dry_run: bool = False


class ExportRequest(BaseModel):
    project: str
    part: str
    offset: int = 0
    limit: int = PAGE_DEFAULT
    since: Optional[str] = None
    until: Optional[str] = None
    include_undated: bool = True


class ProgressRequest(BaseModel):
    job_id: str
    state: str = "running"
    doc: dict = Field(default_factory=dict)


class GrantRemove(BaseModel):
    email: str
    project: Optional[str] = None


def _key_projects(k: dict) -> list[str]:
    allowed = [p["id"] for p in local_projects()]
    if k["projects"]:
        return [p for p in k["projects"] if p in allowed]
    return allowed


def attach(app: FastAPI) -> None:
    try:
        with _conn():
            pass
    except Exception as e:  # noqa: BLE001
        log.warning("sync tables: %s", e)

    @app.get("/sync/me", tags=["sync"])
    def sync_me():
        from app import auth

        p = auth.current()
        if auth.enabled() and p is None:
            raise HTTPException(status_code=403, detail="not a registered KH user")
        maint = p is None or bool(getattr(p, "is_maintainer", False))
        return {"email": getattr(p, "email", None), "maintainer": maint, "role": "maintainer" if maint else "member",
                "auth": auth.enabled()}

    @app.post("/sync/keys", tags=["sync"])
    def sync_keys_create(body: KeyRequest, request: Request):
        url = body.public_url or str(request.base_url).rstrip("/")
        scope = {k: v for k, v in (("parts", body.parts), ("window", body.window), ("members", body.members))
                 if v is not None}
        return make_key(body.label, body.days, body.projects, url, _email() or "local", scope)

    @app.get("/sync/keys", tags=["sync"])
    def sync_keys_list():
        return {"keys": list_keys()}

    @app.delete("/sync/keys/{kid}", tags=["sync"])
    def sync_keys_delete(kid: str):
        if not revoke_key(kid):
            raise HTTPException(status_code=404, detail="no such active key")
        return {"revoked": kid}

    @app.get("/sync/local", tags=["sync"])
    def sync_local():
        return {"projects": local_projects()}

    # -- giver side (public; the key is the credential) --
    @app.get("/sync/peer/hello", tags=["sync"])
    def peer_hello(request: Request):
        check_key(request.headers.get("x-kh-sync-key"))
        return hello()

    @app.get("/sync/peer/manifest", tags=["sync"])
    def peer_manifest(request: Request):
        k = check_key(request.headers.get("x-kh-sync-key"))
        allowed = set(_key_projects(k))
        parts = k["scope"].get("parts") or list(DEFAULT_PARTS)
        out = []
        for p in local_projects():
            if p["id"] in allowed:
                p = dict(p)
                p["parts"] = {x: p["parts"].get(x, 0) for x in parts}
                out.append(p)
        return {"protocol": PROTOCOL, "kid": k["kid"], "projects": out, "window": k["scope"].get("window")}

    @app.post("/sync/peer/export", tags=["sync"])
    def peer_export(body: ExportRequest, request: Request):
        k = check_key(request.headers.get("x-kh-sync-key"))
        if body.project not in _key_projects(k) or vs.is_personal(body.project):
            raise HTTPException(status_code=403, detail="project not offered by this key")
        parts = k["scope"].get("parts") or list(DEFAULT_PARTS + OPTIONAL_PARTS)
        if body.part not in parts:
            raise HTTPException(status_code=403, detail="part not offered by this key")
        win = k["scope"].get("window") or {}
        since = max(filter(None, [body.since, win.get("since")]), default=None)
        until = min(filter(None, [body.until, win.get("until")]), default=None)
        return export_rows(body.project, body.part, body.offset, body.limit, since, until, body.include_undated)

    @app.post("/sync/peer/verify", tags=["sync"])
    def peer_verify(request: Request):
        k = check_key(request.headers.get("x-kh-sync-key"))
        return {"role": "reader", "projects": _key_projects(k)}

    @app.post("/sync/peer/progress", tags=["sync"])
    def peer_progress(body: ProgressRequest, request: Request):
        k = check_key(request.headers.get("x-kh-sync-key"))
        with _conn() as c:
            row = {"job_id": body.job_id, "kid": k["kid"], "state": body.state, "updated": _now(),
                   "doc": json.dumps(body.doc, default=str)}
            old = c.execute("SELECT started FROM sync_given WHERE job_id=?", (body.job_id,)).fetchone()
            row["started"] = old[0] if old else _now()
            sqldb.upsert(c, "sync_given", ["job_id"], row)
        return {"ok": True}

    @app.get("/sync/given", tags=["sync"])
    def sync_given(limit: int = 30):
        with _conn() as c:
            rows = c.execute("SELECT * FROM sync_given ORDER BY updated DESC").fetchall()[:max(0, limit)]
        out = []
        now = datetime.now(timezone.utc)
        for r in rows:
            upd = datetime.fromisoformat((r["updated"] or _now()).replace("Z", "+00:00"))
            stale = r["state"] == "running" and (now - upd).total_seconds() > GIVEN_STALE_S
            out.append({"job_id": r["job_id"], "kid": r["kid"], "state": "stale" if stale else r["state"],
                        "started": r["started"], "updated": r["updated"], "doc": json.loads(r["doc"] or "{}")})
        return {"given": out}

    # -- receiver side --
    @app.post("/sync/connect", tags=["sync"])
    def sync_connect(body: ConnectRequest):
        return connect(body.key)

    @app.post("/sync/pull", tags=["sync"])
    def sync_pull(body: PullRequest):
        if not body.selection:
            raise HTTPException(status_code=422, detail="selection must name at least one project")
        r = start_pull(body.key, body.selection, body.since, body.until, body.include_undated, body.dry_run)
        return JSONResponse(r, status_code=202)

    @app.get("/sync/jobs", tags=["sync"])
    def sync_jobs(limit: int = 30):
        jobs = list_jobs()
        if not _is_maintainer():
            jobs = [j for j in jobs if j["created_by"] == _email()]
        return {"jobs": jobs[:max(0, limit)]}

    @app.get("/sync/jobs/{job_id}", tags=["sync"])
    def sync_job(job_id: str):
        j = get_job(job_id)
        if j is None or (not _is_maintainer() and j["created_by"] != _email()):
            raise HTTPException(status_code=404, detail="no such job")
        return j

    @app.post("/sync/jobs/{job_id}/cancel", tags=["sync"])
    def sync_job_cancel(job_id: str):
        j = get_job(job_id)
        if j is None or (not _is_maintainer() and j["created_by"] != _email()):
            raise HTTPException(status_code=404, detail="no such job")
        if j["state"] in ("queued", "running"):
            _cancel.add(job_id)
        return {"id": job_id, "cancel": j["state"] in ("queued", "running")}

    @app.get("/sync/accounts", tags=["sync"])
    def sync_accounts():
        with _conn() as c:
            rows = c.execute("SELECT * FROM sync_accounts WHERE sent_at IS NULL ORDER BY created_at").fetchall()
        return {"accounts": [{k: r[k] for k in ("email", "password", "created_at", "job_id", "source", "projects",
                                                 "applied", "note")} for r in rows]}

    @app.post("/sync/accounts/{email}/sent", tags=["sync"])
    def sync_account_sent(email: str):
        with _conn() as c:
            cur = c.execute("UPDATE sync_accounts SET sent_at=?, password=NULL WHERE email=?", (_now(), email))
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="no such account")
        return {"email": email, "sent": True}

    @app.get("/sync/grants", tags=["sync"])
    def sync_grants():
        return {"grants": list_grants()}

    @app.post("/sync/grants/remove", tags=["sync"])
    def sync_grants_remove(body: GrantRemove):
        return {"removed": remove_grant(body.email, body.project)}
