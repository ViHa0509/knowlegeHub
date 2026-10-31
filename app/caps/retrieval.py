"""retrieval (spec §6.10): the namespace `/v1/{ns}/context/step`, `/worklog`, `/projects:match`. No tables.

Tiers in pack order: entity, key, key-wildcard, meaning, rules, packs, log (packed with core `step._Pack`).
People lanes (D20: core entity cards, core project knowledge, project worklog) are on unless the manifest sets
`settings.include_people_lanes` / `settings.retrieval.include_people_lanes` false, and take at most
`cross_share` (default 0.4) of the budget.

Providers: `PROVIDERS[tier] = fn(ctx, req) -> [{id, text, score?}]` (the packs module registers "packs"; `req` has
project, keyspace, key, key_sha, query, entities, limit, situation).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import Depends, FastAPI, Request

from app import config, sqldb
from app import ctx_config as C
from app.caps import core
from app.caps.core import CapError, Ctx

NAME = "retrieval"
VERSION = "1.0.0"
STATUS = "beta"
DEFAULT_ON = True
TIERS = ("entity", "key", "key-wildcard", "meaning", "rules", "packs", "log")
PEOPLE_TIERS = {"entity", "log"}
BUDGET_DEFAULT, BUDGET_MIN, BUDGET_MAX = 6000, 200, 20000
QUERY_MAX = 4000
ENTITIES_MAX = 20
LIMIT_DEFAULT, LIMIT_MIN, LIMIT_MAX = 10, 1, 50
CROSS_SHARE = 0.4
WORKLOG_TEXT_MAX = 4000
SUBJECT_MAX = 128
SUBJECT_DEFAULT = "agent worklog"
DESCRIPTOR_MAX = 1000
PEOPLE_ITEMS_LIMIT = 5
CUT_NOTE = " ...(cut; GET the item by id)"
LIMITS = {"budget_chars": [BUDGET_MIN, BUDGET_MAX], "budget_default": BUDGET_DEFAULT, "query_chars": QUERY_MAX,
          "entities": ENTITIES_MAX, "limit": [LIMIT_MIN, LIMIT_MAX], "cross_share": CROSS_SHARE,
          "worklog_text_chars": WORKLOG_TEXT_MAX, "subject_chars": SUBJECT_MAX}
TABLES: list[str] = []
LOCAL_TABLES: list[str] = []
PROVIDERS: dict[str, Callable[[Ctx, dict], list]] = {}
DEAD_VALIDITY = ("superseded", "retired")


def init(c) -> None:  # noqa: ARG001
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _items():
    from app import caps

    return caps.module("items")


def _floor(ctx: Ctx | None) -> float:
    if ctx is not None:
        f = ctx.settings("retrieval").get("floor")
        if isinstance(f, (int, float)) and not isinstance(f, bool):
            return float(f)
    from app import caps

    imp = caps.module("improve")
    if imp is not None and callable(getattr(imp, "param", None)):
        try:
            return float(imp.param("KH_STEP_FLOOR", C.STEP_FLOOR))
        except Exception:  # noqa: BLE001
            pass
    return float(C.STEP_FLOOR)


def people_lanes_on(ctx: Ctx) -> bool:
    s = ctx.settings()
    if s.get("include_people_lanes") is False:
        return False
    return (s.get("retrieval") or {}).get("include_people_lanes") is not False


def cross_share(ctx: Ctx) -> float:
    v = ctx.settings("retrieval").get("cross_share", CROSS_SHARE)
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return CROSS_SHARE


def fold(s: str) -> str:
    t = unicodedata.normalize("NFD", s or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", t).split())


def item_line(row: dict, item_chars: int) -> str:
    key = core.loads(row.get("key_json"), {}) or {}
    vals = "/".join(str(v) for v in key.values())
    unverified = " [unverified]" if row.get("status") != "active" else ""
    title = (row.get("title") or "").strip()
    text = row.get("text") or ""
    if len(text) > item_chars:
        text = text[:item_chars] + CUT_NOTE
    head = f"{row['item_id']} {row['kind']} [{vals}]{unverified}"
    return f"{head} {title}: {text}" if title else f"{head}: {text}"


def _triggered(row: dict, situation: str | None) -> bool:
    trig = core.loads(row.get("triggers_json"), []) or []
    return not trig or (situation is not None and situation in trig)


def _key_rows(ctx: Ctx, sha: str, project: str, situation: str | None, limit: int) -> list[dict]:
    serve_proposed = ctx.settings("retrieval").get("serve_proposed") is not False
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        rows = [{k: r[k] for k in r.keys()} for r in c.execute(
            "SELECT * FROM kh_item WHERE ns=? AND key_sha=? AND status IN ('active','proposed')",
            (ctx.ns, sha)).fetchall()]
    out = []
    for r in rows:
        if r["status"] == "active":
            if r["expires_ms"] is not None and int(r["expires_ms"]) <= now:
                continue
            if not (r["project"] == project or r["scope"] == "cross"):
                continue
        else:
            if not serve_proposed or r["project"] != project:
                continue
        if not _triggered(r, situation):
            continue
        out.append(r)
    out.sort(key=lambda r: (0 if r["status"] == "active" else 1, 0 if r["project"] == project else 1,
                            -int(r["last_confirmed_ms"] or 0), -int(r["updated_ms"] or 0)))
    return out[:limit]


def _meaning_items(ctx: Ctx, query: str, project: str, situation: str | None, limit: int,
                   floor: float) -> list[tuple[float, dict]]:
    mod = _items()
    if mod is None or not query:
        return []
    hits = [h for h in mod.search(ctx.ns, query, limit * 3) if h["score"] >= floor]
    if not hits:
        return []
    ids = [h["item_id"] for h in hits]
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        rows = {r["item_id"]: {k: r[k] for k in r.keys()} for r in c.execute(
            f"SELECT * FROM kh_item WHERE ns=? AND item_id IN ({', '.join('?' for _ in ids)})",
            [ctx.ns, *ids]).fetchall()}
    out = []
    for h in hits:
        r = rows.get(h["item_id"])
        if r is None or r["status"] != "active":
            continue
        if r["expires_ms"] is not None and int(r["expires_ms"]) <= now:
            continue
        if not (r["project"] == project or r["scope"] == "cross"):
            continue
        if not _triggered(r, situation):
            continue
        out.append((h["score"], r))
    return out


def _rule_lines(ctx: Ctx, project: str, entities: list[str], limit: int) -> list[dict]:
    keys = [fold(e) for e in entities if fold(e)]
    if not keys:
        return []
    with sqldb.connect() as c:
        if not sqldb.table_exists(c, "kh_rule"):
            return []
        ph = ", ".join("?" for _ in keys)
        dead = ", ".join("?" for _ in DEAD_VALIDITY)
        q = (f"SELECT rule_id, entity, attribute, cond, effect, validity FROM kh_rule WHERE ns=? AND "
             f"(project=? OR project='') AND validity NOT IN ({dead}) AND (entity_key IN ({ph})")
        params: list = [ctx.ns, project, *DEAD_VALIDITY, *keys]
        if sqldb.table_exists(c, "kh_rule_link"):
            q += f" OR rule_id IN (SELECT rule_id FROM kh_rule_link WHERE ns=? AND to_key IN ({ph}))"
            params += [ctx.ns, *keys]
        q += f") ORDER BY updated_ms DESC LIMIT {int(limit)}"
        rows = c.execute(q, params).fetchall()
    return [{"id": r[0], "text": f"{r[0]} rule [{r[5]}] {r[1]}.{r[2]}: when {r[3] or 'always'} then {r[4]}"}
            for r in rows]


def _provider(name: str) -> Callable | None:
    fn = PROVIDERS.get(name)
    if fn is not None:
        return fn
    from app import caps

    mod = caps.module(name)
    fn = getattr(mod, "retrieval_provider", None) if mod is not None else None
    return fn if callable(fn) else None


# ---------------------------------------------------------------------------
# /context/step
# ---------------------------------------------------------------------------


def _check_step(ctx: Ctx, b: dict) -> dict:
    project = ctx.require_project(b.get("project"))
    mod = _items()
    if mod is None:
        raise CapError("not_found", "the items capability is off")
    key, values, ksha, wild = mod.key_of(ctx.st, b.get("keyspace"), b.get("key"))
    query = b.get("query")
    if query is not None and (not isinstance(query, str) or len(query) > QUERY_MAX):
        raise CapError("invalid", f"query must be a string of at most {QUERY_MAX} characters")
    budget = core.parse_int(b.get("budget_chars"), "budget_chars", BUDGET_DEFAULT, BUDGET_MIN, BUDGET_MAX)
    tiers = b.get("tiers")
    if tiers is not None:
        if not isinstance(tiers, list) or not tiers or any(t not in TIERS for t in tiers):
            raise CapError("invalid", f"tiers must be a non-empty subset of {list(TIERS)}")
    ents = b.get("entities") or []
    if not isinstance(ents, list) or not all(isinstance(e, str) for e in ents) or len(ents) > ENTITIES_MAX:
        raise CapError("invalid", f"entities must be a list of at most {ENTITIES_MAX} strings")
    limit = core.parse_int(b.get("limit"), "limit", LIMIT_DEFAULT, LIMIT_MIN, LIMIT_MAX)
    sit = b.get("situation")
    if sit is not None and not isinstance(sit, str):
        raise CapError("invalid", "situation must be a string")
    return {"project": project, "keyspace": b.get("keyspace"), "key": key, "values": values, "key_sha": ksha,
            "wild_sha": wild, "query": (query or "").strip(), "budget": budget, "tiers": list(tiers or TIERS),
            "entities": [e.strip() for e in ents if e.strip()], "limit": limit, "situation": sit}


def step(ctx: Ctx, body: dict) -> dict:
    ctx.require("retrieval", "read")
    from app import step as core_step

    r = _check_step(ctx, body)
    want = set(r["tiers"])
    project, query, limit = r["project"], r["query"], r["limit"]
    floor = _floor(ctx)
    item_chars = int(C.STEP_ITEM_CHARS)
    people = people_lanes_on(ctx) and bool(project)
    pack = core_step._Pack(r["budget"], r["tiers"])
    cap = int(r["budget"] * cross_share(ctx))
    state = {"people_used": 0}
    failed: list[dict] = []
    served_items: list[str] = []
    item_rows: dict[str, dict] = {}
    dropped_share: list[dict] = []

    def add(tier: str, entry: dict, people_row: bool = False) -> None:
        if people_row:
            if state["people_used"] + len(entry.get("text") or "") > cap:
                dropped_share.append({"tier": tier, "id": entry.get("id"), "reason": "cross_share"})
                return
        before = pack.used
        if pack.add(tier, entry) and people_row:
            state["people_used"] += pack.used - before

    def tier(name: str, fn: Callable[[], None]) -> None:
        if name not in want:
            return
        try:
            fn()
        except CapError:
            raise
        except Exception as e:  # noqa: BLE001
            failed.append({"tier": name, "error": str(e)[:200]})

    def add_item(tier_name: str, row: dict, score: float | None = None) -> None:
        entry = {"id": row["item_id"], "score": None if score is None else round(float(score), 3),
                 "text": item_line(row, item_chars), "status": row["status"], "kind": row["kind"]}
        n = len(pack.items)
        add(tier_name, entry)
        if len(pack.items) > n:
            served_items.append(row["item_id"])
            item_rows[row["item_id"]] = row

    def t_entity() -> None:
        if not people:
            return
        from app import entities

        text = " ".join([query] + r["entities"])
        for card in entities.match(text, [project], int(C.STEP_ENTITY_LIMIT)):
            add("entity", {"id": card["id"], "score": None, "text": entities.line_of(card)}, True)

    def t_key() -> None:
        for row in _key_rows(ctx, r["key_sha"], project, r["situation"], limit):
            add_item("key", row)

    def t_wild() -> None:
        if r["wild_sha"] == r["key_sha"]:
            return
        for row in _key_rows(ctx, r["wild_sha"], project, r["situation"], limit):
            add_item("key-wildcard", row)

    def t_meaning() -> None:
        if not query:
            return
        merged: list[tuple[float, str, Any]] = [(s, "ns", row) for s, row in
                                                 _meaning_items(ctx, query, project, r["situation"], limit, floor)]
        if people:
            from app.embeddings import get_embedder

            for h in core_step._by_meaning(query, "knowledge", project, int(C.STEP_K), floor, get_embedder()):
                merged.append((float(h["score"]), "people", h))
        merged.sort(key=lambda x: -x[0])
        n = 0
        for score, lane, obj in merged:
            if n >= limit:
                break
            if lane == "ns":
                add_item("meaning", obj, score)
            else:
                add("meaning", core_step._row_item(obj, query, item_chars), True)
            n += 1

    def t_rules() -> None:
        from app import caps

        if caps.module("rules") is None or not ctx.allows("rules", "read"):
            return
        for row in _rule_lines(ctx, project, r["entities"], limit):
            add("rules", {"id": row["id"], "score": None, "text": row["text"]})

    def t_packs() -> None:
        fn = _provider("packs")
        if fn is None:
            return
        req = {k: r[k] for k in ("project", "keyspace", "key", "key_sha", "query", "entities", "limit", "situation")}
        for row in (fn(ctx, req) or [])[:limit]:
            add("packs", {"id": row.get("id"), "score": row.get("score"), "text": row.get("text") or ""})

    def t_log() -> None:
        if not people or not query:
            return
        from app.embeddings import get_embedder

        for h in core_step._by_meaning(query, config.HOTLOG_SCOPE, project, int(C.STEP_K), floor, get_embedder()):
            add("log", core_step._row_item(h, query, item_chars), True)

    for name, fn in (("entity", t_entity), ("key", t_key), ("key-wildcard", t_wild), ("meaning", t_meaning),
                     ("rules", t_rules), ("packs", t_packs), ("log", t_log)):
        tier(name, fn)

    serve_id = "sv_" + core.new_id()
    dropped = [{**d, "reason": d.get("reason") or "budget"} for d in pack.dropped] + dropped_share
    tiers_used: list[str] = []
    for it in pack.items:
        if it["tier"] not in tiers_used:
            tiers_used.append(it["tier"])
    served = [{"tier": it["tier"], "id": it["id"], "chars": len(it.get("text") or "")} for it in pack.items]
    out = {
        "serve_id": serve_id, "project": project, "keyspace": r["keyspace"], "key": r["key"], "key_sha": r["key_sha"],
        "pack_text": "\n".join(it.get("text") or "" for it in pack.items), "served": served,
        "item_ids": served_items, "items": pack.items, "tiers": tiers_used, "budget_chars": pack.budget,
        "used_chars": pack.used, "cut_at_budget": bool(pack.dropped), "dropped": dropped, "people_lanes": people,
    }
    if failed:
        out["failed_tiers"] = failed
    core.emit(ctx.ns, "kh.served", {"serve_id": serve_id, "lane": "ns", "project": project, "keyspace": r["keyspace"],
                                    "key": r["key"], "key_sha": r["key_sha"], "served": served,
                                    "item_ids": served_items, "used_chars": pack.used,
                                    "budget_chars": pack.budget}, ctx, project=project)
    return out


# ---------------------------------------------------------------------------
# Reverse direction: namespace items for people's core /context/step (KH_STEP_NS_ITEMS=1)
# ---------------------------------------------------------------------------


def people_items(project: str, text: str, context: str | None = None) -> list[dict]:
    """Active items of consumer namespaces attached to `project` (ns not starting 'kh'): trigger matches (whole
    or last ':'-part of `context`) first, then meaning >= floor; at most 5."""
    mod = _items()
    if mod is None or not project:
        return []
    with sqldb.connect() as c:
        if not sqldb.table_exists(c, "kh_ns"):
            return []
        nss = [r[0] for r in c.execute("SELECT ns, projects_json FROM kh_ns WHERE state='active'").fetchall()
               if not r[0].startswith("kh") and project in (core.loads(r[1], []) or [])]
    out: list[dict] = []
    floor = _floor(None)
    ctxs = {context, (context or "").rpartition(":")[2]} - {None, ""}
    for ns in nss:
        with sqldb.connect() as c:
            now = sqldb.now_ms(c)
            rows = [{k: r[k] for k in r.keys()} for r in c.execute(
                "SELECT * FROM kh_item WHERE ns=? AND status='active' AND (project=? OR scope='cross')",
                (ns, project)).fetchall()]
        live = {r["item_id"]: r for r in rows if r["expires_ms"] is None or int(r["expires_ms"]) > now}
        for r in live.values():
            if ctxs & set(core.loads(r.get("triggers_json"), []) or []):
                out.append({"ns": ns, "id": r["item_id"], "score": None, "text": item_line(r, int(C.STEP_ITEM_CHARS))})
        if text:
            try:
                for h in mod.search(ns, text, PEOPLE_ITEMS_LIMIT * 3):
                    r = live.get(h["item_id"])
                    if r is not None and h["score"] >= floor and not core.loads(r.get("triggers_json"), []):
                        out.append({"ns": ns, "id": r["item_id"], "score": round(h["score"], 3),
                                    "text": item_line(r, int(C.STEP_ITEM_CHARS))})
            except Exception:  # noqa: BLE001
                pass
    seen, res = set(), []
    for x in out:
        if x["id"] not in seen:
            seen.add(x["id"])
            res.append(x)
    return res[:PEOPLE_ITEMS_LIMIT]


def note_people_served(rows: list[dict], serve_id: str | None = None) -> None:
    """Write `kh.served` (lane people) into each namespace that had items served to people."""
    by_ns: dict[str, list[str]] = {}
    for x in rows or []:
        by_ns.setdefault(x["ns"], []).append(x["id"])
    for ns, ids in by_ns.items():
        core.emit(ns, "kh.served", {"serve_id": serve_id or "sv_" + core.new_id(), "lane": "people",
                                    "item_ids": ids})


# ---------------------------------------------------------------------------
# /worklog
# ---------------------------------------------------------------------------


def worklog(ctx: Ctx, body: dict) -> dict:
    ctx.require("worklog", "append")
    project = ctx.require_project(body.get("project"))
    if not project:
        raise CapError("invalid", "a worklog needs a project")
    text, subject = body.get("text"), body.get("subject") or SUBJECT_DEFAULT
    if not isinstance(text, str) or not text.strip() or len(text) > WORKLOG_TEXT_MAX:
        raise CapError("invalid", f"text is required (at most {WORKLOG_TEXT_MAX} characters)")
    if not isinstance(subject, str) or len(subject) > SUBJECT_MAX:
        raise CapError("invalid", f"subject must be a string of at most {SUBJECT_MAX} characters")
    clean = core.apply_body_filters(ctx, "worklog", {"text": text, "subject": subject})
    text, subject = clean["text"], clean["subject"]
    from app import hotlog
    from app import vector_store as vs
    from app.embeddings import EmbedderUnavailable, get_embedder

    try:
        emb = get_embedder()
        vec = emb.encode(text, normalize_embeddings=True)
    except EmbedderUnavailable as e:
        raise CapError("embedder_unavailable", f"no embedding model: {e}")
    row_id = "vec_" + hashlib.sha256(f"{ctx.ns}\x1f{project}\x1f{text}".encode()).hexdigest()[:12]
    owner = vs.own_project(project)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {"knowledge_type": "quick", "scope": config.HOTLOG_SCOPE, "project": owner, "subject": subject,
            "topic": subject, "ts": ts, "author": f"ns:{ctx.ns}/{ctx.principal}", "ns": ctx.ns,
            "run_id": ctx.run_id, "unit_id": ctx.unit_id}
    coll = hotlog.collection() if hotlog.enabled() else vs.get_or_create(
        vs.part_collection_name(config.HOTLOG_SCOPE, owner))
    vs.upsert_one(coll, row_id, vec, text, meta)
    try:
        from app import chunks

        chunks.write_for(row_id, text, meta, owner, emb)
    except Exception:  # noqa: BLE001
        pass
    return {"id": row_id, "project": owner, "subject": subject, "ts": ts}


# ---------------------------------------------------------------------------
# /projects:match
# ---------------------------------------------------------------------------


def projects_match(ctx: Ctx, text: Any) -> dict:
    ctx.require("retrieval", "read")
    if not isinstance(text, str) or not text.strip():
        raise CapError("invalid", "text is required")
    if len(text) > QUERY_MAX:
        raise CapError("invalid", f"text is longer than {QUERY_MAX} characters")
    from app import projects

    allowed = ctx.projects() if ctx.st.projects else list(ctx.caller.projects or [])
    every = "*" in allowed
    hits = [h for h in projects.match_projects(text) if every or h["project"] in allowed]
    weak_only = bool(hits) and bool(hits[0].get("weak"))
    tie = len(hits) > 1 and hits[1].get("term_length") == hits[0].get("term_length") and \
        hits[1].get("matched_on") == hits[0].get("matched_on")
    best = hits[0]["project"] if hits and not weak_only and not tie else None
    matches = []
    for h in hits[:10]:
        p = projects.get_project(h["project"]) or {}
        matches.append({"project": h["project"], "term": h.get("matched_term"), "on": h.get("matched_on"),
                        "weak": bool(h.get("weak")), "descriptor": (p.get("descriptor") or "")[:DESCRIPTOR_MAX],
                        "aliases": list(p.get("aliases") or [])})
    return {"best": best, "ambiguous": bool(hits) and not weak_only and tie, "weak_only": weak_only,
            "matches": matches}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def attach(app: FastAPI) -> None:
    tag = ["v1 retrieval"]

    @app.post("/v1/{ns}/context/step", tags=tag, summary="Keyed, budgeted context pack for one agent step")
    async def post_step(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        body = await core.read_object(request)
        return core.reply(await core.run(step, ctx, body))

    @app.post("/v1/{ns}/worklog", tags=tag, summary="Write an agent worklog into the project's hot log")
    @core.idempotent
    async def post_worklog(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        body = await core.read_object(request)
        return core.reply(await core.run(worklog, ctx, body), status=201)

    @app.post("/v1/{ns}/projects:match", tags=tag, summary="Literal project matcher limited to the namespace")
    async def post_match(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        body = await core.read_object(request)
        return core.reply(await core.run(projects_match, ctx, body.get("text")))
