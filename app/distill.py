"""Distillation (spec §4.2-4.3; served reads §3.3): worklogs -> keyed, reviewable knowledge items.

KH holds no model: a distiller (human-driven CLI, the external worker, or in-process learn-back) pulls batches
(`GET /distill/next`) and submits items (`POST /distill/submit`); KH validates in code (consent, leak gate,
dedupe, relation handling) and serves live items by key (`served`, used by /context/step and /turn/context).

Public helpers for other areas:
    served(lane, action, context, entities, limit, text=None, model=None) -> (items, key)
    exact_first(reads) -> [item]        line_of(item, with_sources=False) -> str
    serve_next(project, limit, caller=None, lanes=None) / submit(body, caller=None)
    approve(project, ids, share, caller=None) / reject(project, ids, reason) / reopen(project, ids)
    contexts_of(project) -> [{tag, description, source}]
    leak(text, project, strict=None, share=False) -> str | None
Routes are plain `def` (thread pool) so encoding never blocks the event loop.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import numpy as np
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import config, sqldb
from app import vector_store as vs
from app.projects import fold

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (read at call time: tests monkeypatch the environment)
# ---------------------------------------------------------------------------

KINDS = ("lesson", "fact", "procedure", "habit", "pattern", "policy_proposal")
SUBMIT_KINDS = ("lesson", "fact", "procedure", "habit", "policy_proposal")
STATUSES = ("proposed", "active", "contested", "superseded", "withdrawn")
LIVE = ("active", "contested")
OPEN = ("proposed", "active", "contested")
TOPIC = "distilled"
EVERY = "every project"
CUT_SUFFIX = " ...(cut; read the row by id)"

DEFAULT_SIGNAL_SEED = (
    "decision:decided,decision,decide,chose,agreed,quyet dinh,chon;"
    "failure:failed,failure,error,broken,bug,crash,rollback,revert,outage,loi,hong,that bai;"
    "newcomer:onboarding,newcomer,first time,nguoi moi;"
    "procedure:steps,procedure,runbook,how to,cach lam,quy trinh,huong dan;"
    "correction:wrong,mistake,correction,actually,fixed,nham,sua lai"
)
DEFAULT_LEAK_PATTERNS = [
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    r"(?i)\b(?:password|secret|token|api_key)\s*[:=]\s*(?=[^\s]*\d)[^\s]{8,}",
    r"\b(?:sk|pk|ghp|gho|xoxb)[-_][A-Za-z0-9_-]{8,}",
    r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}(?:\.[A-Za-z0-9_-]*)?",
]
DEFAULT_SHARE_LEAK_PATTERNS = [
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    r"(?i)\b[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|cloud|ondemand\.com|internal|local|vn)\b",
]


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in (config._env(name, "") or "").split(",") if x.strip()]


def _patterns(name: str, default: list[str]) -> list[str]:
    raw = config._env(name)
    if not raw:
        return list(default)
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return [str(x) for x in v]
    except ValueError:
        pass
    return [p for p in raw.split("\n") if p.strip()]


def batch_default() -> int:
    return config._int("KH_DISTILL_BATCH", 20)


def scan_max() -> int:
    return config._int("KH_DISTILL_SCAN_MAX", 400)


def sample_pct() -> float:
    return _param("KH_DISTILL_SAMPLE_PCT", config._float("KH_DISTILL_SAMPLE_PCT", 10))


def confirm_floor() -> float:
    return _param("KH_DISTILL_CONFIRM", config._float("KH_DISTILL_CONFIRM", 0.85))


def same_floor() -> float:
    return config._float("KH_DISTILL_SAME", 0.95)


def log_chars() -> int:
    return config._int("KH_DISTILL_LOG_CHARS", 2000)


def neighbour_chars() -> int:
    return config._int("KH_DISTILL_NEIGHBOUR_CHARS", 6000)


def title_chars() -> int:
    return config._int("KH_DISTILL_TITLE_CHARS", 120)


def item_chars() -> int:
    return config._int("KH_DISTILL_ITEM_CHARS", 600)


def shared_project() -> str:
    return config._env("KH_DISTILL_SHARED_PROJECT", "lesson-learned") or "lesson-learned"


def consented(project: str | None) -> bool:
    return bool(project) and project in _env_list("KH_DISTILL_PROJECTS")


def own_project_lenient(project: str | None) -> bool:
    return bool(project) and project in _env_list("KH_DISTILL_OWN_PROJECTS")


def worker_mode() -> str:
    return (config._env("KH_DISTILL_WORKER", "off") or "off").lower()


def _param_raw(name: str, default: Any) -> Any:
    """Tunables via app.caps.improve.param (only when that module is on); else the default."""
    try:
        from app.caps import improve  # type: ignore

        return improve.param(name, default)
    except Exception:  # noqa: BLE001
        return default


def _param(name: str, default: float) -> float:
    try:
        return float(_param_raw(name, default))
    except (TypeError, ValueError):
        return default


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# Hook set by the /v1 metrics capability (§6): returns the calendar-month worker spend in USD, or raises.
WORKER_SPEND_FN: Optional[Callable[[], float]] = None


# ---------------------------------------------------------------------------
# Caller
# ---------------------------------------------------------------------------


def _principal():
    try:
        from app import auth

        return auth.current()
    except Exception:  # noqa: BLE001
        return None


def is_worker(p=None) -> bool:
    p = p if p is not None else _principal()
    if p is None:
        return False
    return bool(getattr(p, "ns", None)) and (getattr(p, "svc_role", None) == "distiller"
                                             or getattr(p, "principal_id", None) == "worker")


def is_maintainer(p=None) -> bool:
    p = p if p is not None else _principal()
    return p is None or bool(getattr(p, "is_maintainer", False))


def caller_name(p=None) -> str:
    p = p if p is not None else _principal()
    if p is None:
        return "local"
    return getattr(p, "email", None) or getattr(p, "principal_id", None) or "unknown"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

_ready: set[str] = set()


def _m1(c):
    sqldb.create_table(
        c, "distill_cursor",
        "CREATE TABLE IF NOT EXISTS distill_cursor (project TEXT PRIMARY KEY, last_ts REAL, last_ids TEXT, "
        "signal_ids TEXT, topics TEXT, updated_at TEXT)",
        "CREATE COLUMN TABLE distill_cursor (project NVARCHAR(256) PRIMARY KEY, last_ts DOUBLE, last_ids NCLOB, "
        "signal_ids NCLOB, topics NCLOB, updated_at NVARCHAR(40))")
    sqldb.create_table(
        c, "distill_batch",
        "CREATE TABLE IF NOT EXISTS distill_batch (batch_id TEXT PRIMARY KEY, project TEXT, log_ids TEXT, "
        "state TEXT, created_at TEXT, claimed INTEGER DEFAULT 0, cost_usd REAL, closed INTEGER DEFAULT 0)",
        "CREATE COLUMN TABLE distill_batch (batch_id NVARCHAR(64) PRIMARY KEY, project NVARCHAR(256), "
        "log_ids NCLOB, state NCLOB, created_at NVARCHAR(40), claimed INTEGER DEFAULT 0, cost_usd DOUBLE, "
        "closed INTEGER DEFAULT 0)")
    sqldb.create_table(
        c, "distill_signal",
        "CREATE TABLE IF NOT EXISTS distill_signal (word TEXT PRIMARY KEY, kind TEXT, source TEXT, reason TEXT, "
        "hits INTEGER DEFAULT 0, added_at TEXT)",
        "CREATE COLUMN TABLE distill_signal (word NVARCHAR(64) PRIMARY KEY, kind NVARCHAR(32), "
        "source NVARCHAR(128), reason NVARCHAR(500), hits INTEGER DEFAULT 0, added_at NVARCHAR(40))")
    sqldb.create_table(
        c, "distill_context",
        "CREATE TABLE IF NOT EXISTS distill_context (project TEXT NOT NULL, tag TEXT NOT NULL, description TEXT, "
        "source TEXT, added_at TEXT, PRIMARY KEY (project, tag))",
        "CREATE COLUMN TABLE distill_context (project NVARCHAR(256) NOT NULL, tag NVARCHAR(64) NOT NULL, "
        "description NVARCHAR(500), source NVARCHAR(128), added_at NVARCHAR(40), PRIMARY KEY (project, tag))")


def _seed_signals(c):
    raw = config._env("KH_DISTILL_SIGNAL_SEED", DEFAULT_SIGNAL_SEED) or ""
    for part in raw.split(";"):
        if ":" not in part:
            continue
        kind, words = part.split(":", 1)
        for w in words.split(","):
            w = fold(w)
            if w:
                sqldb.insert_new(c, "distill_signal", {"word": w, "kind": kind.strip(), "source": "seed",
                                                       "reason": "", "hits": 0, "added_at": _now()})


def ensure_tables(c=None) -> None:
    def run(cc):
        sqldb.migrate(cc, "distill", [(1, _m1), (2, _seed_signals)])

    if c is not None:
        run(c)
        return
    with sqldb.connect() as cc:
        run(cc)


def _conn():
    c = sqldb.connect()
    key = sqldb._location(c)
    if key not in _ready:
        ensure_tables(c)
        c.commit()
        _ready.add(key)
    return c


def _j(v, default):
    if v is None or v == "":
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Embedding helpers
# ---------------------------------------------------------------------------


def _model(model=None):
    if model is not None:
        return model
    from app.embeddings import get_embedder

    return get_embedder()


def encode(text: str, model=None) -> list[float]:
    v = _model(model).encode(text, normalize_embeddings=True)
    return v.tolist() if hasattr(v, "tolist") else list(v)


def cosine(a, b) -> float:
    return float(np.dot(np.asarray(a, dtype=float), np.asarray(b, dtype=float)))


# ---------------------------------------------------------------------------
# Signals, contexts, cursor
# ---------------------------------------------------------------------------


def signals() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT word, kind, source, reason, hits, added_at FROM distill_signal ORDER BY word").fetchall()
    return [{"word": r["word"], "kind": r["kind"], "source": r["source"], "reason": r["reason"],
             "hits": int(r["hits"] or 0), "added_at": r["added_at"]} for r in rows]


def _signal_words() -> list[tuple[str, str]]:
    words = [(s["word"], s["kind"]) for s in signals()]
    try:
        extra = _param_raw("signal_words", None)
        if isinstance(extra, list):
            words += [(fold(w), "improve") for w in extra if fold(w)]
    except Exception:  # noqa: BLE001
        pass
    return words


def _has_word(folded: str, word: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(word) + r"(?![a-z0-9])", folded) is not None


def norm_context(ctx: str | None) -> str:
    c = (ctx or "").strip()
    if not c or c == "-":
        return "-"
    if c.startswith("ent__"):
        return c
    return fold(c).replace(" ", "-")


def contexts_of(project: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT tag, description, source FROM distill_context WHERE project=? ORDER BY tag",
                         (project,)).fetchall()
    return [{"tag": r["tag"], "description": r["description"] or "", "source": r["source"] or ""} for r in rows]


def add_context(project: str, tag: str, description: str = "", source: str = "") -> dict:
    t = norm_context(tag)
    if t == "-" or t.startswith("ent__") or not (2 <= len(t) <= 64):
        return {"tag": tag, "result": "rejected", "reason": "tag must be 2..64 characters"}
    with _conn() as c:
        ok = sqldb.insert_new(c, "distill_context", {"project": project, "tag": t,
                                                     "description": (description or "")[:500],
                                                     "source": (source or "")[:128], "added_at": _now()})
    return {"tag": t, "result": "added" if ok else "exists"}


def cursor_row(project: str) -> dict:
    with _conn() as c:
        r = c.execute("SELECT last_ts, last_ids, signal_ids, topics, updated_at FROM distill_cursor WHERE project=?",
                      (project,)).fetchone()
    if r is None:
        return {"last_ts": 0.0, "last_ids": [], "signal_ids": [], "topics": [], "updated_at": None, "exists": False}
    return {"last_ts": float(r["last_ts"] or 0.0), "last_ids": _j(r["last_ids"], []),
            "signal_ids": _j(r["signal_ids"], []), "topics": _j(r["topics"], []), "updated_at": r["updated_at"],
            "exists": True}


def _move_cursor(c, project: str, st: dict) -> None:
    """Forward-only: UPDATE ... WHERE last_ts <= ?, insert if absent."""
    vals = (float(st.get("last_ts") or 0.0), json.dumps(st.get("last_ids") or []),
            json.dumps((st.get("signal_ids") or [])[-500:]), json.dumps(st.get("topics") or []), _now())
    cur = c.execute("UPDATE distill_cursor SET last_ts=?, last_ids=?, signal_ids=?, topics=?, updated_at=? "
                    "WHERE project=? AND last_ts <= ?", (*vals, project, vals[0]))
    if not cur.rowcount or cur.rowcount <= 0:
        exists = c.execute("SELECT 1 FROM distill_cursor WHERE project=?", (project,)).fetchone()
        if not exists:
            sqldb.insert_new(c, "distill_cursor", {"project": project, "last_ts": vals[0], "last_ids": vals[1],
                                                   "signal_ids": vals[2], "topics": vals[3], "updated_at": vals[4]})


# ---------------------------------------------------------------------------
# Policy table access (§3.7.2 tables, read/written by SQL; owned by §3)
# ---------------------------------------------------------------------------


def _policy_table_ok(c) -> bool:
    return sqldb.table_exists(c, "policy_actions") and sqldb.table_exists(c, "policy_rules")


def menu(project: str | None) -> list[str]:
    """Action labels of the menu for a project (tiers: project, every project)."""
    try:
        from app.policy_actions import store as pstore  # type: ignore

        return sorted({a.get("action") for a in pstore.menu(project) if a.get("action")})
    except Exception as e:  # noqa: BLE001
        log.debug("policy_actions.store.menu unavailable (%s); reading by SQL", e)
    try:
        with sqldb.connect() as c:
            if not _policy_table_ok(c):
                return []
            tiers = [t for t in (project, EVERY) if t]
            q = f"SELECT DISTINCT action FROM policy_actions WHERE project IN ({', '.join('?' for _ in tiers)})"
            return sorted({r[0] for r in c.execute(q, tiers).fetchall()})
    except Exception as e:  # noqa: BLE001
        log.debug("policy menu unavailable: %s", e)
        return []


def common_actions() -> list[dict]:
    """Every-project actions with description and steps (for /promote/next)."""
    out = []
    try:
        with sqldb.connect() as c:
            if not _policy_table_ok(c):
                return []
            acts = c.execute("SELECT action, description FROM policy_actions WHERE project=? ORDER BY action",
                             (EVERY,)).fetchall()
            for a in acts:
                steps = c.execute("SELECT sub_action, seq, content FROM policy_rules WHERE project=? AND action=? "
                                  "ORDER BY seq, sub_action", (EVERY, a["action"])).fetchall()
                out.append({"action": a["action"], "description": a["description"] or "",
                            "steps": [{"sub_action": s["sub_action"], "seq": int(s["seq"] or 0),
                                       "content": s["content"] or ""} for s in steps]})
    except Exception as e:  # noqa: BLE001
        log.debug("common actions unavailable: %s", e)
    return out


def step_seq(action: str, sub_action: str, project: str = EVERY) -> int | None:
    try:
        with sqldb.connect() as c:
            if not _policy_table_ok(c):
                return None
            r = c.execute("SELECT seq FROM policy_rules WHERE project=? AND action=? AND sub_action=?",
                          (project, action, sub_action)).fetchone()
            return int(r[0] or 0) if r else None
    except Exception:  # noqa: BLE001
        return None


def set_rule(action: str, sub_action: str, content: str, project: str, seq: int | None = None) -> dict:
    """policy_store.set_rule equivalent: upsert one step of an action in a tier."""
    if seq is None:
        seq = step_seq(action, sub_action, project) or 0
    try:
        from app.policy_actions import store as pstore  # type: ignore

        fn = getattr(pstore, "set_rule", None)
    except Exception:  # noqa: BLE001
        fn = None
    if callable(fn):
        return fn(project, action, sub_action, content, seq=int(seq))
    with sqldb.connect() as c:
        if not _policy_table_ok(c):
            raise HTTPException(status_code=409, detail="policy action table unavailable")
        sqldb.upsert(c, "policy_rules", ["project", "action", "sub_action"],
                     {"project": project, "action": action, "sub_action": sub_action, "seq": int(seq),
                      "clauses": "", "content": content, "updated_at": _now()})
    return {"project": project, "action": action, "sub_action": sub_action, "seq": seq}


# ---------------------------------------------------------------------------
# Entity cards / names (for people, catalog and the leak gate)
# ---------------------------------------------------------------------------


def _entity_rows(project: str | None = None) -> list[tuple[str, dict]]:
    names = [vs.part_collection_name("entity", project)] if project else \
        [n for n in vs.list_collection_names() if n.startswith("kh_entity_")]
    out = []
    for n in names:
        h = vs.get_existing(n)
        if h is None:
            continue
        try:
            out += vs.meta_rows_vt(h, None)
        except Exception:  # noqa: BLE001
            continue
    return out


def people_cards(project: str) -> list[dict]:
    out = []
    for rid, m in _entity_rows(project):
        if (m.get("entity_type") or m.get("type")) != "person":
            continue
        out.append({"id": rid, "name": m.get("name"), "aliases": _j(m.get("aliases_json"), []), "role": m.get("role")})
    return out


def _names(types: set[str]) -> set[str]:
    out = set()
    for _rid, m in _entity_rows(None):
        if (m.get("entity_type") or m.get("type")) not in types:
            continue
        for t in [m.get("name"), *_j(m.get("aliases_json"), [])]:
            f = fold(str(t or ""))
            if len(f) >= 3:
                out.add(f)
    return out


def _project_names() -> set[str]:
    from app import projects

    out = set()
    for pid, e in projects.list_projects().items():
        for t in [pid, *(e.get("aliases") or [])]:
            f = fold(str(t or ""))
            if len(f) >= 3:
                out.add(f)
    return out


# ---------------------------------------------------------------------------
# Leak gate
# ---------------------------------------------------------------------------


def _redaction_first(text: str) -> str | None:
    try:
        from app.caps import redact  # type: ignore

        sc = redact.default_scanner()
        found = sc.scan(text) if hasattr(sc, "scan") else sc(text)
        if found:
            f = found[0]
            return f.get("match") if isinstance(f, dict) else str(f)
    except Exception:  # noqa: BLE001
        return None
    return None


def leak(text: str, project: str | None, strict: bool | None = None, share: bool = False) -> str | None:
    """First leaking fragment, or None. strict = hosts/IPs (default: unless the project is own-lenient);
    names: persons always, orgs too for non-own projects; share/promote also every project id and alias."""
    t = text or ""
    for p in _patterns("KH_DISTILL_LEAK_PATTERNS", DEFAULT_LEAK_PATTERNS):
        try:
            m = re.search(p, t)
        except re.error:
            continue
        if m:
            return m.group(0)
    r = _redaction_first(t)
    if r:
        return r
    own = own_project_lenient(project)
    if strict is None:
        strict = share or not own
    if strict:
        for p in _patterns("KH_DISTILL_SHARE_LEAK_PATTERNS", DEFAULT_SHARE_LEAK_PATTERNS):
            try:
                m = re.search(p, t)
            except re.error:
                continue
            if m:
                return m.group(0)
    folded = " " + fold(t) + " "
    types = {"person"} if (own and not share) else {"person", "org"}
    names = _names(types)
    if share:
        names |= _project_names()
    for n in sorted(names, key=len, reverse=True):
        if " " + n + " " in folded:
            return n
    return None


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


def _coll(project: str, create: bool = True):
    return vs.scope_collection("knowledge", project) if create else vs.get_existing(
        vs.part_collection_name("knowledge", project))


def dkey(project: str, kind: str, action: str, context: str, entity_key: str) -> str:
    return "dk_" + hashlib.sha256(f"{project}|{kind}|{action}|{context}|{entity_key}".encode()).hexdigest()[:12]


def _item(row: dict) -> dict:
    m = dict(row.get("metadata") or {})
    return {
        "id": row["vector_id"], "kind": m.get("kind") or "-", "status": m.get("status"),
        "action": m.get("action") or "-", "context": m.get("context") or "-",
        "title": m.get("title") or "", "text": row.get("source_text") or "",
        "entities": _j(m.get("entities_json"), []), "sources": _j(m.get("sources_json"), []),
        "evidence_count": int(m.get("evidence_count") or 0), "last_confirmed": m.get("last_confirmed") or m.get("ts"),
        "distilled_at": m.get("distilled_at"), "dkey": m.get("dkey"), "metadata": m,
    }


def _rows_where(project: str, where: dict | None) -> list[dict]:
    h = _coll(project, create=False)
    if h is None:
        return []
    return vs.get_vt(h, where=where)


def items_of(project: str, statuses=OPEN) -> list[dict]:
    rows = _rows_where(project, {"status": {"$in": list(statuses)}})
    return [_item(r) for r in rows]


def get_item(project: str, item_id: str) -> dict | None:
    h = _coll(project, create=False)
    if h is None:
        return None
    rows = vs.get_ids_vt(h, [item_id])
    return _item(rows[0]) if rows else None


def line_of(item: dict, with_sources: bool = False) -> str:
    flag = " [contested]" if item.get("status") == "contested" else ""
    title = item.get("title") or ""
    body = f"{title}: {item.get('text') or ''}" if title else (item.get("text") or "")
    s = f"{item['id']} {item.get('kind') or '-'}{flag} [{item.get('action') or '-'} / {item.get('context') or '-'}] {body}"
    if with_sources and item.get("sources"):
        s += f" (sources: {', '.join(item['sources'][:5])})"
    return s


def _write(project: str, item_id: str, meta: dict, title: str, text: str, model=None, vec=None) -> None:
    status = meta.get("status")
    meta = dict(meta)
    meta.update({"scope": "knowledge", "project": project, "topic": TOPIC, "title": title,
                 "knowledge_type": "quick" if status in LIVE else status, "ts": _now()})
    v = vec if vec is not None else encode(f"{title}. {text}", model)
    vs.upsert_one(_coll(project), item_id, v, text, meta)


def _update_meta(project: str, item_id: str, changes: dict) -> bool:
    h = _coll(project, create=False)
    if h is None:
        return False
    rows = vs.get_ids_vt(h, [item_id])
    if not rows:
        return False
    m = dict(rows[0]["metadata"] or {})
    m.update(changes)
    m = {k: v for k, v in m.items() if v is not None}
    st = m.get("status")
    if st:
        m["knowledge_type"] = "quick" if st in LIVE else st
    h.update(ids=[item_id], metadatas=[m])
    return True


def _new_id(project: str, key: str) -> str:
    h = _coll(project, create=False)
    if h is None:
        return key
    if not vs.get_ids_vt(h, [key]):
        return key
    n = len(h.get(where={"dkey": {"$eq": key}}, include=[]).get("ids") or [])
    cand = f"{key}_{n + 1}"
    while vs.get_ids_vt(h, [cand]):
        n += 1
        cand = f"{key}_{n + 1}"
    return cand


def _embedding_of(project: str, item_id: str):
    h = _coll(project, create=False)
    if h is None:
        return None
    res = h.get(ids=[item_id], include=["embeddings"])
    e = (res.get("embeddings") or [None])[0]
    return e


def _nearest(project: str, vec, kinds: list[str] | None, statuses=OPEN) -> tuple[dict | None, float]:
    h = _coll(project, create=False)
    if h is None:
        return None, 0.0
    where = {"status": {"$in": list(statuses)}}
    if kinds:
        where = vs.and_where(where, {"kind": {"$in": kinds}})
    hits = vs.query_vt(h, vec, 1, where)
    if not hits:
        return None, 0.0
    return _item(hits[0]), float(hits[0]["score"])


# ---------------------------------------------------------------------------
# Step A: next batch
# ---------------------------------------------------------------------------


def log_lanes(project: str) -> list:
    return vs.read_lanes(config.HOTLOG_SCOPE, project, None)


def _confirmer(project: str, text: str, words: list[str], model=None) -> str | None:
    if not words:
        return None
    vec = encode(text[:2000], model)
    item, score = _nearest(project, vec, None, OPEN)
    if item is None or score < confirm_floor():
        return None
    ft = fold(f"{item.get('title')} {item.get('text')}")
    if all(_has_word(ft, w) for w in words):
        return item["id"]
    return None


def next_batch(project: str, limit: int, lanes: list | None = None, model=None) -> dict:
    from app import hotlog

    cur = cursor_row(project)
    last_ts, last_ids = cur["last_ts"], set(cur["last_ids"])
    lanes = lanes if lanes is not None else log_lanes(project)
    stubs: dict[str, tuple[float, Any, dict | None, dict]] = {}
    for coll, where in lanes:
        try:
            for rid, m in vs.meta_rows_vt(coll, where):
                ts = hotlog.parse_ts(m.get("ts"))
                if ts < last_ts or (ts == last_ts and rid in last_ids):
                    continue
                if rid not in stubs:
                    stubs[rid] = (ts, coll, where, m)
        except Exception as e:  # noqa: BLE001
            log.debug("lane read failed: %s", e)
    order = sorted(stubs, key=lambda i: (stubs[i][0], i))
    total = len(order)
    scan = order[:max(0, scan_max())]
    words = _signal_words()
    topics = list(cur["topics"])
    topic_set = set(topics)
    signal_ids = list(cur["signal_ids"])
    signal_set = set(signal_ids)
    picked, passed, confirms, hits = [], 0, {}, {}
    examined: list[str] = []
    stop = False
    for k in range(0, len(scan), 50):
        chunk = scan[k:k + 50]
        texts: dict[str, str] = {}
        by_coll: dict[int, list] = {}
        for rid in chunk:
            by_coll.setdefault(id(stubs[rid][1]), []).append(rid)
        for ids in by_coll.values():
            coll = stubs[ids[0]][1]
            for r in vs.get_ids_vt(coll, ids):
                texts[r["vector_id"]] = r.get("source_text") or ""
        for rid in chunk:
            if len(picked) >= limit:
                stop = True
                break
            ts, _coll_h, _w, m = stubs[rid]
            examined.append(rid)
            text = texts.get(rid, "")
            ft = fold(text)
            why, found = [], []
            for w, _kind in words:
                if w and _has_word(ft, w) and w not in found:
                    found.append(w)
                    why.append(f"word:{w}")
            topic = m.get("topic")
            new_topic = bool(topic) and topic not in topic_set
            if new_topic:
                why.append("new-topic")
                topic_set.add(topic)
                topics.append(topic)
            for ref in re.findall(r"follows\s+(vec_[0-9a-f]+)", text):
                if ref in signal_set:
                    why.append("follows-signal")
                    break
            if not why and int(hashlib.sha256(rid.encode()).hexdigest()[:8], 16) % 100 < sample_pct():
                why.append("sample")
            if not why:
                passed += 1
                continue
            for w in found:
                hits[w] = hits.get(w, 0) + 1
            if found:
                signal_ids.append(rid)
                signal_set.add(rid)
            if found and not new_topic:
                try:
                    target = _confirmer(project, text, found, model)
                except Exception as e:  # noqa: BLE001
                    log.debug("confirmer failed: %s", e)
                    target = None
                if target:
                    confirms.setdefault(target, []).append(rid)
                    continue
            picked.append({"id": rid, "ts": m.get("ts"), "topic": topic, "why": why, "text": text[:log_chars()],
                           "actor": m.get("actor")})
        if stop:
            break
    remaining = total - len(examined)
    batch_id = None
    if examined:
        mx = max(stubs[i][0] for i in examined)
        at_max = [i for i in examined if stubs[i][0] == mx]
        new_last_ids = sorted(set(at_max) | (last_ids if mx == last_ts else set()))
        state = {"last_ts": mx, "last_ids": new_last_ids, "signal_ids": signal_ids[-500:], "topics": topics,
                 "hits": hits, "confirms": confirms}
        batch_id = "db_" + uuid.uuid4().hex
        with _conn() as c:
            sqldb.insert_new(c, "distill_batch", {"batch_id": batch_id, "project": project,
                                                  "log_ids": json.dumps([p["id"] for p in picked]),
                                                  "state": json.dumps(state), "created_at": _now(), "claimed": 0,
                                                  "cost_usd": None, "closed": 0})
    return {"project": project, "batch_id": batch_id, "logs": picked, "passed_without_signal": passed,
            "confirmed_in_code": {k: len(v) for k, v in confirms.items()}, "remaining_after": remaining,
            "done": remaining == 0}


RULES = (
    "Write one item per lesson, fact, procedure, habit or policy_proposal with lasting value only (a decision and "
    "its reason, a failure and its fix, steps that worked, how a role works, a rule people keep asking for); "
    "several logs about one thing make one item citing all of them in sources. Kinds: lesson = symptom, cause, "
    "fix; fact = one sentence; procedure = numbered steps; habit = how a role behaves; policy_proposal = the rule "
    "itself with target_action and target_sub_action. Never write kind pattern. English only. Title <= 120 "
    "characters, text <= 600. Put people only as entity card ids (ent__...) in entities and use role aliases "
    "(Dev, PO, QC, reviewer) in the text; never write secrets or e-mail addresses. Action must be a catalog "
    "label or '-'; context a catalog tag, an entity id, or '-', or declare a new tag under contexts. Use updates "
    "for the same item newer or confirmed (cite the new logs) and contradicts for an opposite claim on the same "
    "subject. Read each log's prompts for the need behind the work but never quote or cite them. An empty items "
    "list is valid."
)


def neighbours(project: str) -> list[str]:
    items = items_of(project, OPEN)
    items.sort(key=lambda i: str(i.get("last_confirmed") or ""), reverse=True)
    out, used = [], 0
    cap = neighbour_chars()
    for it in items:
        line = line_of(it)
        if used + len(line) > cap:
            break
        out.append(line)
        used += len(line) + 1
    return out


def catalog(project: str) -> dict:
    return {"kinds": [k for k in KINDS if k != "pattern"], "actions": menu(project), "contexts": contexts_of(project)}


class BudgetError(Exception):
    """429 {error: "budget", detail, month_usd, cap_usd} (handler registered by attach)."""

    def __init__(self, detail: str, month_usd, cap_usd):
        super().__init__(detail)
        self.body = {"error": "budget", "detail": detail, "month_usd": month_usd, "cap_usd": cap_usd}


def month_budget_check() -> None:
    """Server-side lock 3: 429 for worker tokens once the month's worker spend reaches the cap (fails closed)."""
    cap = config._float("KH_WORKER_CAP_MONTH_USD", 60)
    try:
        if WORKER_SPEND_FN is None:
            raise RuntimeError("worker spend ledger unavailable")
        spent = float(WORKER_SPEND_FN())
    except Exception as e:  # noqa: BLE001
        raise BudgetError(f"month spend unreadable: {e}", None, cap)
    if spent >= cap:
        raise BudgetError("worker month cap reached", round(spent, 4), cap)


def _consent(project: str) -> None:
    if not consented(project):
        raise HTTPException(status_code=403,
                            detail=f"project '{project}' has not consented to distillation (KH_DISTILL_PROJECTS)")


def serve_next(project: str, limit: int | None = None, caller=None, lanes: list | None = None, model=None) -> dict:
    _consent(project)
    if is_worker(caller):
        month_budget_check()
    limit = max(1, min(int(limit or batch_default()), 100))
    out = next_batch(project, limit, lanes, model)
    try:
        from app.eventing.prompts import attach_prompts

        attach_prompts(out["logs"])
    except Exception:  # noqa: BLE001
        for lg in out["logs"]:
            lg.pop("actor", None)
            lg.setdefault("prompts", [])
    out["neighbours"] = neighbours(project)
    out["people"] = people_cards(project)
    out["catalog"] = catalog(project)
    out["rules"] = RULES
    return out


# ---------------------------------------------------------------------------
# Step B: submit
# ---------------------------------------------------------------------------


class ContextIn(BaseModel):
    tag: str
    description: str = ""


class ItemIn(BaseModel):
    kind: str = ""
    action: str = "-"
    context: str = "-"
    title: str = ""
    text: str = ""
    entities: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    updates: Optional[str] = None
    contradicts: Optional[str] = None
    target_action: Optional[str] = None
    target_sub_action: Optional[str] = None
    share: Optional[bool] = None


class SubmitRequest(BaseModel):
    project: str
    batch_id: Optional[str] = None
    items: list[dict] = Field(default_factory=list)
    contexts: list[dict] = Field(default_factory=list)
    signal_words: list[Any] = Field(default_factory=list)
    distilled_by: str = ""
    dry_run: bool = False
    cost_usd: Optional[Any] = None


def _batch(batch_id: str | None) -> dict | None:
    if not batch_id:
        return None
    with _conn() as c:
        r = c.execute("SELECT batch_id, project, log_ids, state, claimed, cost_usd, closed FROM distill_batch "
                      "WHERE batch_id=?", (batch_id,)).fetchone()
    if r is None:
        return None
    return {"batch_id": r["batch_id"], "project": r["project"], "log_ids": _j(r["log_ids"], []),
            "state": _j(r["state"], {}), "claimed": int(r["claimed"] or 0), "cost_usd": r["cost_usd"],
            "closed": int(r["closed"] or 0)}


def _valid_sources(project: str, batch_ids: list[str], cited: set[str], lanes: list | None) -> set[str]:
    valid = set(batch_ids)
    extra = [s for s in cited if s not in valid]
    if extra:
        for coll, where in (lanes if lanes is not None else log_lanes(project)):
            try:
                for r in vs.get_ids_vt(coll, extra, where):
                    valid.add(r["vector_id"])
            except Exception:  # noqa: BLE001
                continue
    return valid


def _merge_sources(old: list, new: list) -> list:
    out = list(old)
    for s in new:
        if s not in out:
            out.append(s)
    return out[-50:]


def _submit_one(n: int, raw: dict, project: str, menu_labels: list[str], ctx_tags: set[str], valid: set[str],
                distilled_by: str, dry: bool, model=None) -> dict:
    def rej(reason):
        return {"n": n, "result": "rejected", "reason": reason}

    try:
        it = ItemIn(**{k: v for k, v in (raw or {}).items() if k in ItemIn.model_fields})
    except Exception as e:  # noqa: BLE001
        return rej(f"invalid item: {e}")
    kind = (it.kind or "").strip()
    if kind not in SUBMIT_KINDS:
        return rej(f"kind must be one of {', '.join(SUBMIT_KINDS)}")
    action = (it.action or "-").strip() or "-"
    if action != "-" and action not in menu_labels:
        return rej(f"unknown action '{action}' (use a catalog label or '-')")
    context = norm_context(it.context)
    if context != "-" and not context.startswith("ent__") and context not in ctx_tags:
        return rej(f"unknown context '{context}' (use a catalog tag, an entity id, or '-'; declare new tags "
                   f"under contexts)")
    title, text = (it.title or "").strip(), (it.text or "").strip()
    if not (1 <= len(title) <= title_chars()):
        return rej(f"title must be 1..{title_chars()} characters")
    if not (1 <= len(text) <= item_chars()):
        return rej(f"text must be 1..{item_chars()} characters")
    sources = [s for s in it.sources if s]
    if not sources:
        return rej("sources must not be empty")
    bad = [s for s in sources if s not in valid]
    if bad:
        return rej(f"unknown sources: {', '.join(bad[:5])}")
    lk = leak(f"{title}\n{text}", project)
    if lk:
        return rej(f"leak gate: '{lk}'")
    ents = sorted({e for e in it.entities if e})
    if any(not e.startswith("ent__") for e in ents):
        return rej("entities must be entity card ids (ent__...)")
    if kind == "policy_proposal":
        if not it.target_action or it.target_action not in menu_labels:
            return rej("policy_proposal needs a known target_action")
        if not (it.target_sub_action or "").strip():
            return rej("policy_proposal needs a non-empty target_sub_action")
    if it.updates and it.contradicts:
        return rej("give updates or contradicts, not both")
    ekey = ",".join(ents) if ents else "-"
    key = dkey(project, kind, action, context, ekey)
    now = _now()
    base = {"kind": kind, "action": action, "context": context, "entities_json": json.dumps(ents),
            "entity_key": ekey, "dkey": key, "distilled_by": distilled_by, "distilled_at": now,
            "last_confirmed": now}
    if kind == "policy_proposal":
        base["target_action"] = it.target_action
        base["target_sub_action"] = it.target_sub_action.strip()
    if it.updates:
        tgt = get_item(project, it.updates)
        if tgt is None or tgt["status"] not in OPEN:
            return rej(f"updates: '{it.updates}' is not a live item of this project")
        if dry:
            return {"n": n, "result": "updated", "id": tgt["id"]}
        srcs = _merge_sources(tgt["sources"], sources)
        new_src = [s for s in sources if s not in tgt["sources"]]
        meta = dict(tgt["metadata"])
        meta.update(base)
        meta.update({"status": tgt["status"], "sources_json": json.dumps(srcs),
                     "evidence_count": max(int(meta.get("evidence_count") or 0) + len(new_src), len(srcs)),
                     "first_seen": meta.get("first_seen") or now})
        _write(project, tgt["id"], meta, title, text, model)
        return {"n": n, "result": "updated", "id": tgt["id"]}
    vec = encode(f"{title}. {text}", model)
    if it.contradicts:
        tgt = get_item(project, it.contradicts)
        if tgt is None or tgt["status"] not in OPEN:
            return rej(f"contradicts: '{it.contradicts}' is not a live item of this project")
        new_id = _new_id(project, key)
        if not dry:
            meta = {**base, "status": "contested", "sources_json": json.dumps(sources[-50:]),
                    "evidence_count": len(sources), "first_seen": now}
            _write(project, new_id, meta, title, text, model, vec)
            _update_meta(project, tgt["id"], {"status": "contested"})
        return {"n": n, "result": "contested", "id": new_id, "with": [tgt["id"]]}
    near, score = _nearest(project, vec, [kind], OPEN)
    if near is not None and score >= same_floor():
        if not dry:
            srcs = _merge_sources(near["sources"], sources)
            new_src = [s for s in sources if s not in near["sources"]]
            _update_meta(project, near["id"], {
                "sources_json": json.dumps(srcs), "last_confirmed": now,
                "evidence_count": max(int(near["evidence_count"]) + len(new_src), len(srcs))})
        return {"n": n, "result": "merged", "id": near["id"], "score": round(score, 4)}
    new_id = _new_id(project, key)
    if not dry:
        meta = {**base, "status": "proposed", "sources_json": json.dumps(sources[-50:]),
                "evidence_count": len(sources), "first_seen": now}
        _write(project, new_id, meta, title, text, model, vec)
    return {"n": n, "result": "accepted", "id": new_id}


def _apply_confirms(project: str, confirms: dict) -> None:
    now = _now()
    for item_id, logs in (confirms or {}).items():
        it = get_item(project, item_id)
        if it is None:
            continue
        srcs = _merge_sources(it["sources"], logs)
        new_src = [s for s in logs if s not in it["sources"]]
        _update_meta(project, item_id, {"sources_json": json.dumps(srcs), "last_confirmed": now,
                                        "evidence_count": max(it["evidence_count"] + len(new_src), len(srcs))})


def _add_signal_word(raw: Any, project: str, source: str) -> dict:
    if isinstance(raw, dict):
        word, kind, reason = raw.get("word"), raw.get("kind") or "learned", raw.get("reason") or ""
    else:
        word, kind, reason = raw, "learned", ""
    w = fold(str(word or ""))
    if not (3 <= len(w) <= 40):
        return {"word": word, "result": "rejected", "reason": "word must be 3..40 characters"}
    lk = leak(str(word), project) or leak(w, project)  # gate the raw word: folding strips '@' and '.'
    if lk:
        return {"word": w, "result": "rejected", "reason": f"leak gate: '{lk}'"}
    with _conn() as c:
        ok = sqldb.insert_new(c, "distill_signal", {"word": w, "kind": str(kind)[:32], "source": source[:128],
                                                    "reason": str(reason)[:500], "hits": 0, "added_at": _now()})
    return {"word": w, "result": "added" if ok else "exists"}


def submit(body: SubmitRequest | dict, caller=None, lanes: list | None = None, model=None,
           caller_label: str | None = None) -> dict:
    b = body if isinstance(body, SubmitRequest) else SubmitRequest(**body)
    project = b.project
    _consent(project)
    worker = is_worker(caller)
    if worker and not (b.distilled_by or "").startswith("worker:"):
        raise HTTPException(status_code=400, detail='worker submits must send distilled_by = "worker:<model>"')
    cost = None
    if b.cost_usd is not None:
        try:
            cost = float(b.cost_usd)
        except (TypeError, ValueError):
            cost = -1
        if isinstance(b.cost_usd, bool) or not (0 <= cost <= 1000):
            raise HTTPException(status_code=400, detail="cost_usd must be a number 0..1000")
    batch = _batch(b.batch_id)
    if batch is None or batch["closed"] or batch["project"] != project:
        raise HTTPException(status_code=409, detail="unknown or already submitted batch")
    forced_dry = worker and worker_mode() == "dry"
    dry = bool(b.dry_run) or forced_dry
    by = b.distilled_by or caller_label or caller_name(caller)
    ctx_res = []
    for cx in b.contexts or []:
        if not isinstance(cx, dict):
            continue
        if dry:
            t = norm_context(cx.get("tag"))
            ok = t != "-" and 2 <= len(t) <= 64
            ctx_res.append({"tag": t, "result": "added" if ok else "rejected"} if ok else
                           {"tag": cx.get("tag"), "result": "rejected", "reason": "tag must be 2..64 characters"})
        else:
            ctx_res.append(add_context(project, cx.get("tag") or "", cx.get("description") or "", by))
    sw_res = []
    for sw in b.signal_words or []:
        if dry:
            raw_w = str(sw.get("word") if isinstance(sw, dict) else sw or "")
            w = fold(raw_w)
            ok = 3 <= len(w) <= 40 and not leak(raw_w, project) and not leak(w, project)
            sw_res.append({"word": w, "result": "added" if ok else "rejected"})
        else:
            sw_res.append(_add_signal_word(sw, project, by))
    ctx_tags = {c["tag"] for c in contexts_of(project)}
    if dry:
        ctx_tags |= {r["tag"] for r in ctx_res if r.get("result") == "added"}
    menu_labels = menu(project)
    cited = {s for it in b.items for s in ((it or {}).get("sources") or []) if isinstance(s, str)}
    valid = _valid_sources(project, batch["log_ids"], cited, lanes)
    results = [_submit_one(i, it, project, menu_labels, ctx_tags, valid, by, dry, model)
               for i, it in enumerate(b.items or [])]
    rejected = any(r["result"] == "rejected" for r in results)
    st = batch["state"]
    cursor_ts = cursor_row(project)["last_ts"]
    batch_open = True
    if dry:
        if not batch["claimed"]:
            with _conn() as c:
                c.execute("DELETE FROM distill_batch WHERE batch_id=? AND claimed=0", (batch["batch_id"],))
            batch_open = False
    else:
        with _conn() as c:
            cur = c.execute("UPDATE distill_batch SET claimed=1 WHERE batch_id=? AND claimed=0", (batch["batch_id"],))
            first = bool(cur.rowcount and cur.rowcount > 0)
            if first:
                _move_cursor(c, project, st)
                for w, n in (st.get("hits") or {}).items():
                    c.execute("UPDATE distill_signal SET hits=hits+? WHERE word=?", (int(n), w))
        if first:
            _apply_confirms(project, st.get("confirms") or {})
        total_cost = (float(batch["cost_usd"] or 0) + (cost or 0)) if cost is not None else batch["cost_usd"]
        with _conn() as c:
            if rejected:
                st["submitted"] = True
                c.execute("UPDATE distill_batch SET state=?, cost_usd=? WHERE batch_id=?",
                          (json.dumps(st), total_cost, batch["batch_id"]))
            elif total_cost and float(total_cost) > 0:
                c.execute("UPDATE distill_batch SET closed=1, cost_usd=? WHERE batch_id=?",
                          (total_cost, batch["batch_id"]))
                batch_open = False
            else:
                c.execute("DELETE FROM distill_batch WHERE batch_id=?", (batch["batch_id"],))
                batch_open = False
        cursor_ts = cursor_row(project)["last_ts"]
    out = {"project": project, "dry_run": dry, "results": results, "contexts": ctx_res, "signal_words": sw_res,
           "confirmed_in_code": {k: len(v) for k, v in (st.get("confirms") or {}).items()}, "cursor": cursor_ts,
           "batch_open": batch_open, "cost_usd": cost}
    if forced_dry:
        out["forced_dry"] = True
    return out


# ---------------------------------------------------------------------------
# Step C: review
# ---------------------------------------------------------------------------


def pending(project: str) -> dict:
    items = items_of(project, ("proposed", "contested"))
    items.sort(key=lambda i: str(i.get("distilled_at") or ""), reverse=True)
    return {"project": project, "count": len(items), "lines": [line_of(i, with_sources=True) for i in items]}


def _share(item: dict, model=None) -> dict:
    target = shared_project()
    lk = leak(f"{item['title']}\n{item['text']}", target, strict=True, share=True)
    if lk:
        return {"id": item["id"], "result": f"refused: not general ('{lk}')"}
    context = item["context"] if not str(item["context"]).startswith("ent__") else "-"
    vec = encode(f"{item['title']}. {item['text']}", model)
    near, score = _nearest(target, vec, [item["kind"]], OPEN)
    now = _now()
    if near is not None and score >= same_floor():
        _update_meta(target, near["id"], {"last_confirmed": now, "status": "active",
                                          "evidence_count": near["evidence_count"] + 1})
        return {"id": item["id"], "result": "merged", "shared_id": near["id"]}
    key = dkey(target, item["kind"], item["action"], context, "-")
    new_id = _new_id(target, key)
    meta = {"kind": item["kind"], "action": item["action"], "context": context, "entities_json": "[]",
            "entity_key": "-", "dkey": key, "status": "active", "sources_json": json.dumps(item["sources"][-50:]),
            "evidence_count": max(item["evidence_count"], 1), "first_seen": now, "last_confirmed": now,
            "distilled_by": item["metadata"].get("distilled_by"), "distilled_at": now,
            "origin_project": item["metadata"].get("project")}
    _write(target, new_id, meta, item["title"], item["text"], model, vec)
    return {"id": item["id"], "result": "shared", "shared_id": new_id}


def approve(project: str, ids: list[str], share: list[str] | None = None, caller=None, model=None) -> dict:
    results, shared = [], []
    share = set(share or [])
    for i in ids or []:
        it = get_item(project, i)
        if it is None:
            results.append({"id": i, "result": "not found"})
            continue
        if it["status"] not in ("proposed", "contested"):
            results.append({"id": i, "result": f"not reviewable (status {it['status']})"})
            continue
        m = it["metadata"]
        if it["kind"] == "policy_proposal":
            ta, tsa = m.get("target_action"), m.get("target_sub_action")
            if m.get("tier") == "common":
                seq = m.get("seq")
                seq = int(seq) if seq not in (None, "") else None
                set_rule(ta, tsa, it["text"], project=EVERY, seq=seq)
                applied = f"{EVERY}:{ta}/{tsa}"
            else:
                set_rule(ta, tsa, it["text"], project=project)
                applied = f"{project}:{ta}/{tsa}"
            _update_meta(project, i, {"status": "superseded", "applied_to": applied})
            results.append({"id": i, "result": "applied", "applied_to": applied})
            continue
        _update_meta(project, i, {"status": "active", "last_confirmed": _now()})
        if it["dkey"]:
            for other in _rows_where(project, {"$and": [{"dkey": {"$eq": it["dkey"]}},
                                                        {"status": {"$eq": "contested"}}]}):
                if other["vector_id"] != i:
                    _update_meta(project, other["vector_id"], {"status": "superseded", "superseded_by": i})
        results.append({"id": i, "result": "active"})
        if i in share:
            fresh = get_item(project, i)
            shared.append(_share(fresh, model))
    return {"project": project, "results": results, "shared": shared}


def reject(project: str, ids: list[str], reason: str = "") -> dict:
    results = []
    for i in ids or []:
        ok = get_item(project, i) is not None and _update_meta(project, i, {"status": "withdrawn",
                                                                            "reject_reason": (reason or "")[:500]})
        results.append({"id": i, "result": "withdrawn" if ok else "not found"})
    return {"project": project, "results": results}


def reopen(project: str, ids: list[str]) -> dict:
    results = []
    for i in ids or []:
        it = get_item(project, i)
        if it is None:
            results.append({"id": i, "result": "not found"})
            continue
        m = dict(it["metadata"])
        m.pop("superseded_by", None)
        m["status"] = "proposed"
        m["knowledge_type"] = "proposed"
        h = _coll(project, create=False)
        h.update(ids=[i], metadatas=[m])
        results.append({"id": i, "result": "proposed"})
    return {"project": project, "results": results}


# ---------------------------------------------------------------------------
# Rekey, patterns
# ---------------------------------------------------------------------------


def rekey_candidates(project: str, limit: int = 50) -> dict:
    h = _coll(project, create=False)
    rows = []
    if h is not None:
        for r in vs.get_vt(h):
            m = r["metadata"] or {}
            if m.get("action") or m.get("topic") == TOPIC or m.get("chunk_of") or m.get("kind") == "chunk":
                continue
            rows.append(r)
    out = [{"id": r["vector_id"], "text": (r.get("source_text") or "")[:600],
            "kind": (r["metadata"] or {}).get("kind"), "topic": (r["metadata"] or {}).get("topic"),
            "category": (r["metadata"] or {}).get("category")} for r in rows[:max(1, int(limit))]]
    return {"project": project, "count": len(out), "remaining": len(rows), "rows": out, "catalog": catalog(project)}


def rekey(project: str, keys: list[dict], contexts: list[dict] | None = None) -> dict:
    ctx_res = [add_context(project, c.get("tag") or "", c.get("description") or "", "rekey")
               for c in (contexts or []) if isinstance(c, dict)]
    tags = {c["tag"] for c in contexts_of(project)}
    labels = menu(project)
    h = _coll(project, create=False)
    results = []
    by = caller_name()
    for k in keys or []:
        rid = (k or {}).get("id")
        rows = vs.get_ids_vt(h, [rid]) if (h is not None and rid) else []
        if not rows:
            results.append({"id": rid, "result": "not found"})
            continue
        m = dict(rows[0]["metadata"] or {})
        if k.get("skip"):
            m.update({"action": "-", "context": "-"})
            h.update(ids=[rid], metadatas=[m])
            results.append({"id": rid, "result": "skipped"})
            continue
        action = (k.get("action") or "-").strip() or "-"
        context = norm_context(k.get("context"))
        if action != "-" and action not in labels:
            results.append({"id": rid, "result": "rejected", "reason": f"unknown action '{action}'"})
            continue
        if context != "-" and not context.startswith("ent__") and context not in tags:
            results.append({"id": rid, "result": "rejected", "reason": f"unknown context '{context}'"})
            continue
        ents = sorted({e for e in (k.get("entities") or []) if isinstance(e, str) and e.startswith("ent__")})
        m.update({"action": action, "context": context, "entities_json": json.dumps(ents),
                  "entity_key": ",".join(ents) if ents else "-", "status": "active", "knowledge_type": "quick",
                  "rekeyed_by": by, "rekeyed_at": _now(), "last_confirmed": m.get("last_confirmed") or m.get("ts")})
        h.update(ids=[rid], metadatas=[m])
        results.append({"id": rid, "result": "keyed"})
    return {"project": project, "results": results, "contexts": ctx_res}


def patterns(project: str | None, days: int = 90) -> dict:
    import time as _t

    from app import hotlog

    projects = [project] if project else _env_list("KH_DISTILL_PROJECTS")
    since = _t.time() - days * 86400
    fails = [w for w, k in _signal_words() if k == "failure"]
    out = []
    for p in projects:
        if not consented(p):
            continue
        n = nf = 0
        topics: dict[str, int] = {}
        for coll, where in log_lanes(p):
            for r in vs.get_vt(coll, where):
                m = r["metadata"] or {}
                if hotlog.parse_ts(m.get("ts")) < since:
                    continue
                n += 1
                ft = fold(r.get("source_text") or "")
                if any(_has_word(ft, w) for w in fails):
                    nf += 1
                    t = m.get("topic") or "-"
                    topics[t] = topics.get(t, 0) + 1
        top = sorted(topics.items(), key=lambda x: (-x[1], x[0]))[:5]
        out.append({"project": p, "logs": n, "failure_logs": nf, "failure_rate": round(nf / n, 4) if n else 0.0,
                    "top_failing_topics": [{"topic": t, "count": c} for t, c in top]})
    return {"days": days, "projects": out}


# ---------------------------------------------------------------------------
# Step D: serving by key
# ---------------------------------------------------------------------------


def _cut(t: str) -> str:
    return t if len(t) <= 600 else t[:600] + CUT_SUFFIX


def _live_by_key(lane: str, where: dict) -> list[dict]:
    return [_item(r) for r in _rows_where(lane, vs.and_where({"status": {"$in": list(LIVE)}}, where))]


def _bonus() -> float | None:
    raw = (config._env("KH_STEP_CONTEXT_BONUS", "0.05") or "0.05").lower()
    if raw == "off":
        return None
    try:
        return float(raw)
    except ValueError:
        return 0.05


def served(lane: str | None, action: str | None, context: str | None, entities: list[str] | None = None,
           limit: int = 6, text: str | None = None, model=None):
    """Live items of one project lane by key. Returns (items, key); items carry `key`."""
    if not lane:
        return [], ""
    action = (action or "").strip() or None
    ctx = norm_context(context) if context else None
    if ctx == "-":
        ctx = None
    ents = set(entities or [])
    h = _coll(lane, create=False)
    if h is None:
        return [], ""
    items: list[dict] = []
    key = ""
    bonus = _bonus()
    if action and ctx and text and model is not None and bonus is not None:
        where = {"$and": [{"action": {"$eq": action}}, {"status": {"$in": list(LIVE)}}]}
        hits = vs.query_vt(h, encode(text, model), max(limit * 3, 20), where)
        scored = []
        for hit in hits:
            it = _item(hit)
            s = float(hit["score"]) + (bonus if it["context"] == ctx else 0.0)
            scored.append((s, it))
        scored.sort(key=lambda x: x[0], reverse=True)
        items = [it for _s, it in scored]
        key = "action, context lifts" if any(it["context"] == ctx for it in items[:limit]) else "action"
    else:
        tries = []
        if action and ctx:
            tries.append(("action+context", {"$and": [{"action": {"$eq": action}}, {"context": {"$eq": ctx}}]}))
        if action:
            tries.append(("action", {"action": {"$eq": action}}))
        elif ctx:
            tries.append(("context", {"context": {"$eq": ctx}}))
        for label, where in tries:
            if text and model is not None:
                hits = vs.query_vt(h, encode(text, model), max(limit * 3, 20),
                                   vs.and_where({"status": {"$in": list(LIVE)}}, where))
                found = [_item(x) for x in hits]
            else:
                found = _live_by_key(lane, where)
                found.sort(key=lambda i: str(i.get("last_confirmed") or ""), reverse=True)
            if found:
                items, key = found, label
                break
    if ents:
        items.sort(key=lambda i: 0 if ents & set(i.get("entities") or []) else 1)
    items = items[:max(0, int(limit))]
    out = []
    for it in items:
        it = dict(it)
        it["text"] = _cut(it["text"])
        it["key"] = key
        it["lane"] = lane
        out.append(it)
    return out, key


def exact_first(reads: list) -> list[dict]:
    """reads = [(items, key)] per lane; when any lane matched action+context, drop other lanes' action-only
    fallback. Returns the flat item list (deduped by id, lane order kept)."""
    norm = []
    for r in reads or []:
        if isinstance(r, tuple) and len(r) >= 2:
            norm.append((r[0] or [], r[1] or ""))
    exact = any(k == "action+context" and items for items, k in norm)
    out, seen = [], set()
    for items, k in norm:
        if exact and k == "action":
            continue
        for it in items:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            out.append(it)
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class ReviewRequest(BaseModel):
    project: str
    ids: list[str] = Field(default_factory=list)
    share: list[str] = Field(default_factory=list)
    reason: str = ""


class RekeyRequest(BaseModel):
    project: str
    keys: list[dict] = Field(default_factory=list)
    contexts: list[dict] = Field(default_factory=list)


class SignalsRequest(BaseModel):
    add: list[Any] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class CursorReset(BaseModel):
    project: str


def attach(app: FastAPI, log_lanes_fn=None) -> None:  # noqa: ARG001
    try:
        ensure_tables()
    except Exception as e:  # noqa: BLE001
        log.warning("distill tables: %s", e)

    def _model_of():
        return getattr(app.state, "model", None)

    @app.exception_handler(BudgetError)
    async def _budget(_request, exc: BudgetError):
        return JSONResponse(exc.body, status_code=429)

    @app.get("/distill/next", tags=["distill"])
    def distill_next(project: str, limit: int = Query(None, ge=1, le=100)):
        return serve_next(project, limit, _principal(), model=_model_of())

    @app.post("/distill/submit", tags=["distill"])
    def distill_submit(body: SubmitRequest):
        return submit(body, _principal(), model=_model_of())

    @app.get("/distill/pending", tags=["distill"])
    def distill_pending(project: str):
        return pending(project)

    @app.post("/distill/approve", tags=["distill"])
    def distill_approve(body: ReviewRequest):
        return approve(body.project, body.ids, body.share, model=_model_of())

    @app.post("/distill/reject", tags=["distill"])
    def distill_reject(body: ReviewRequest):
        return reject(body.project, body.ids, body.reason)

    @app.post("/distill/reopen", tags=["distill"])
    def distill_reopen(body: ReviewRequest):
        return reopen(body.project, body.ids)

    @app.get("/distill/rekey", tags=["distill"])
    def distill_rekey_get(project: str, limit: int = Query(20, ge=1, le=100)):
        return rekey_candidates(project, limit)

    @app.post("/distill/rekey", tags=["distill"])
    def distill_rekey_post(body: RekeyRequest):
        return rekey(body.project, body.keys, body.contexts)

    @app.get("/distill/patterns", tags=["distill"])
    def distill_patterns(project: Optional[str] = None, days: int = Query(90, ge=1, le=730)):
        if project:
            _consent(project)
        return patterns(project, days)

    @app.get("/distill/signals", tags=["distill"])
    def distill_signals():
        words = signals()
        return {"count": len(words), "words": words}

    @app.post("/distill/signals", tags=["distill"])
    def distill_signals_post(body: SignalsRequest):
        added = [_add_signal_word(w, "", caller_name()) for w in body.add]
        removed = []
        with _conn() as c:
            for w in body.remove:
                cur = c.execute("DELETE FROM distill_signal WHERE word=?", (fold(w),))
                if cur.rowcount and cur.rowcount > 0:
                    removed.append(fold(w))
        words = signals()
        return {"added": added, "removed": removed, "count": len(words)}

    @app.get("/distill/cursor", tags=["distill"])
    def distill_cursor(project: str):
        cur = cursor_row(project)
        return {"project": project, "consented": consented(project), "last_ts": cur["last_ts"],
                "topics": len(cur["topics"]), "contexts": contexts_of(project)}

    @app.post("/distill/cursor/reset", tags=["distill"])
    def distill_cursor_reset(body: CursorReset):
        _consent(body.project)
        with _conn() as c:
            cur = c.execute("DELETE FROM distill_cursor WHERE project=?", (body.project,))
            n = int(cur.rowcount or 0)
        return {"project": body.project, "reset": n > 0}
