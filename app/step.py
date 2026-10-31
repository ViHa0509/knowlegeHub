"""`POST /context/step`: one bounded pack per work step (spec §3.3). Route group `step`.

Tiers in pack order: entity, distilled, personal-lesson, agents, daily, log-recent, knowledge, log.
The optional `agents` / `daily` tiers need capability modules that this rebuild does not ship (both default off).
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from app import config
from app import ctx_config as C
from app import ctx_util as U
from app.ctx_util import fold

log = logging.getLogger(__name__)

TIERS = ("entity", "distilled", "personal-lesson", "agents", "daily", "log-recent", "knowledge", "log")
_MIN_ITEM_CHARS = C.MIN_ITEM_CHARS
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_REF = re.compile(r"\b(?:[A-Z][A-Z0-9]{1,9}-\d{1,6}|PR #\d+|MR !?\d+)\b")
OVERRIDABLE = {
    "KH_STEP_FLOOR": ("STEP_FLOOR", float), "KH_STEP_KNOW_ENOUGH": ("STEP_KNOW_ENOUGH", float),
    "KH_STEP_ITEM_CHARS": ("STEP_ITEM_CHARS", int), "KH_STEP_ENTITY_LIMIT": ("STEP_ENTITY_LIMIT", int),
    "KH_STEP_RECENT_LIMIT": ("STEP_RECENT_LIMIT", int), "KH_STEP_KEY_LIMIT": ("STEP_KEY_LIMIT", int),
    "KH_STEP_CONTEXT_BONUS": ("STEP_CONTEXT_BONUS", float), "KH_STEP_CONTEXT_LIMIT": ("STEP_CONTEXT_LIMIT", int),
    "KH_STEP_THREAD_LIMIT": ("STEP_THREAD_LIMIT", int),
}

HINTS = {
    "entity-not-loaded": "named in the pack but not loaded; repeat the step naming them (entities=[ids]) or "
                         "GET /entities/match?text=<name> if the answer depends on them",
    "reference-not-looked-up": "tickets or pull requests named in the pack; take one more step with the reference "
                               "if the answer depends on it",
    "cut-at-budget": "rows left out at the budget; read one with POST /vector/scroll {ids} if needed",
    "log-skipped": "repeat the step with include_log=true for history, or when knowledge seems stale",
    "newer-log": "a newer work log exists than any packed one; GET /log/recent?project=<id>&limit=1 if the latest "
                 "status matters",
}


class WithItem(BaseModel):
    id: str = Field(..., min_length=1, max_length=64)
    text: str = Field(..., min_length=1, max_length=2000)


class StepRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    project: Optional[str] = None
    budget_chars: int = Field(6000, ge=500, le=40000)
    k: int = Field(6, ge=1, le=20)
    include_log: bool = False
    action: Optional[str] = Field(None, max_length=120)
    context: Optional[str] = Field(None, max_length=120)
    entities: list[str] = Field(default_factory=list, max_length=10)
    only_tiers: Optional[list[str]] = None
    with_items: Optional[list[WithItem]] = Field(None, max_length=10)
    overrides: Optional[dict[str, Any]] = None


def time_window(text: str) -> tuple[bool, Optional[str], Optional[float]]:
    m = _DATE.search(text or "")
    if m:
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return True, f"{m.group(1)}T00:00:00Z", (d + timedelta(days=1)).timestamp()
        except ValueError:
            pass
    hay = " " + fold(text) + " "
    for w in C.STEP_TIME_WORDS:
        f = fold(w)
        if f and (" " + f + " ") in hay:
            return True, None, None
    return False, None, None


class _Pack:
    def __init__(self, budget: int, only_tiers: Optional[list[str]] = None):
        self.budget = int(budget)
        self.only = set(only_tiers) if only_tiers else None
        self.used = 0
        self.full = False
        self.items: list[dict] = []
        self.ids: set[str] = set()
        self.dropped: list[dict] = []

    def add(self, tier: str, item: dict) -> bool:
        if self.only is not None and tier not in self.only:
            return False
        iid = item.get("id")
        if iid in self.ids:
            return False
        if self.full:
            self.dropped.append({"tier": tier, "id": iid, "score": item.get("score")})
            return False
        text = item.get("text") or ""
        room = self.budget - self.used
        if len(text) > room:
            self.full = True
            if room < _MIN_ITEM_CHARS:
                self.dropped.append({"tier": tier, "id": iid, "score": item.get("score")})
                return False
            item = {**item, "text": text[:room], "truncated": True, "text_len": item.get("text_len") or len(text)}
            text = item["text"]
        self.ids.add(iid)
        self.used += len(text)
        self.items.append({"tier": tier, **item})
        return True


def _row_item(hit: dict, query: str, item_chars: int) -> dict:
    from app.retriever import clip_hit

    h = clip_hit(dict(hit), item_chars, query)
    out = {"id": h["vector_id"], "score": round(float(h["score"]), 3) if h.get("score") is not None else None,
           "text": h.get("source_text") or ""}
    if h.get("truncated"):
        out["truncated"] = True
        out["text_len"] = h.get("text_len")
    if h.get("piece_at") is not None:
        out["piece_at"] = h["piece_at"]
    ts = (h.get("metadata") or {}).get("ts")
    if ts:
        out["ts"] = ts
    return out


def _by_meaning(text: str, scope: str, project: Optional[str], k: int, floor: float, model) -> list[dict]:
    from app import auth
    from app.retriever import query_quick_knowledge

    hits = query_quick_knowledge(text, k=k, model=model, scope=scope, project=project)
    return [h for h in hits if h["score"] >= floor and auth.row_visible(h.get("metadata"), h["vector_id"])]


def _distill():
    try:
        from app import distill

        return distill
    except Exception:  # noqa: BLE001
        return None


def _key_read(lane: str, action, context, entities, limit, text, model):
    d = _distill()
    fn = getattr(d, "served", None) if d is not None else None
    if not callable(fn):
        return [], ""
    res = fn(lane, action, context, entities, limit, text, model)
    if isinstance(res, tuple):
        return list(res[0] or []), res[1] or ""
    return list(res or []), ""


def _contexts(lane: str) -> list[str]:
    d = _distill()
    if d is None:
        return []
    for name in ("contexts_of", "contexts", "context_tags"):
        fn = getattr(d, name, None)
        if callable(fn):
            out = fn(lane)
            return [c if isinstance(c, str) else (c.get("context") or c.get("tag") or "") for c in out or []]
    return []


def _exact_first(results: dict[str, tuple[list, str]], action, context) -> list[tuple[str, dict, str]]:
    """[(lane, item, key)]: if any lane answered with the exact key action+context, other lanes' action-only
    fallbacks are dropped."""
    d = _distill()
    fn = getattr(d, "exact_first", None) if d is not None else None
    if callable(fn):
        try:
            lane_of = {id(it): ln for ln, (items, _k) in results.items() for it in items}
            return [(it.get("lane") or lane_of.get(id(it)), it, it.get("key") or "")
                    for it in fn(list(results.values()))]
        except Exception as e:  # noqa: BLE001
            log.debug("distill.exact_first failed (%s); local fallback used", e)
    act_only = "action"
    exact = any(key == "action+context" and items for items, key in results.values())
    out = []
    for lane, (items, key) in results.items():
        if exact and key == act_only:
            continue
        out.extend((lane, it, key) for it in items)
    return out


def _line_of(item: dict, with_sources: bool) -> str:
    d = _distill()
    fn = getattr(d, "line_of", None) if d is not None else None
    if callable(fn):
        try:
            return fn(item, with_sources=with_sources)
        except TypeError:
            return fn(item)
    kind = item.get("kind") or "item"
    tag = " [contested]" if item.get("status") == "contested" else ""
    s = (f"{item.get('id')} {kind}{tag} [{item.get('action') or '-'} / {item.get('context') or '-'}] "
         f"{item.get('title') or ''}: {item.get('text') or ''}")
    if with_sources and item.get("sources"):
        s += " (sources: " + ", ".join(list(item["sources"])[:5]) + ")"
    return s


async def _safe(coro, default):
    try:
        return await coro
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("step read failed: %s", e)
        return default


def _withheld(rows: list[dict]) -> list[dict]:
    try:
        from app.caps import learn  # type: ignore

        fn = getattr(learn, "withhold", None)
        if callable(fn):
            return fn(rows)
    except Exception:  # noqa: BLE001
        pass
    return rows


async def build(body: StepRequest, model, recent: Optional[Callable[..., dict]] = None) -> dict:
    bench = bool(body.only_tiers or body.with_items or body.overrides)
    maint = U.is_maintainer()
    if bench and not maint:
        raise HTTPException(status_code=403, detail="only_tiers, with_items and overrides are for maintainers")
    if body.only_tiers:
        bad = [t for t in body.only_tiers if t not in TIERS]
        if bad:
            raise HTTPException(status_code=422, detail=f"unknown tier(s) {bad}; tiers are {list(TIERS)}")
    P = {v[0]: getattr(C, v[0]) for v in OVERRIDABLE.values()}
    for name, val in (body.overrides or {}).items():
        if name not in OVERRIDABLE:
            raise HTTPException(status_code=422, detail=f"unknown override '{name}'; allowed: {sorted(OVERRIDABLE)}")
        attr, typ = OVERRIDABLE[name]
        try:
            P[attr] = typ(val)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail=f"bad value for override '{name}'")
    floor = float(P["STEP_FLOOR"])
    item_chars = int(P["STEP_ITEM_CHARS"])

    project = body.project.strip() if body.project and body.project.strip() else None
    if project and not U.may(project, "project.read"):
        raise HTTPException(status_code=403, detail="missing permission project.read on this project")
    k = body.k or C.STEP_K
    pack = _Pack(body.budget_chars or C.STEP_BUDGET_CHARS, body.only_tiers)
    text = body.text
    is_time, since, until = time_window(text)
    scope = [project] if project else U.readable_projects()
    lesson = C.TURN_LESSON_PROJECT
    lanes: list[Optional[str]] = [project]
    if lesson != project and U.may(lesson, "project.read"):
        lanes.append(lesson)
    personal = U.personal_tier()
    from app import entities

    def t(fn, *a):
        return asyncio.to_thread(fn, *a)

    def recent_rows(limit: int, since_: Optional[str], until_: Optional[float]) -> list[dict]:
        if recent is None:
            return []
        r = recent(limit=limit, project=project, since=since_, until=None if until_ is None else str(until_))
        return list((r or {}).get("hits") or [])

    keyed = bool(body.action or body.context)
    real_lanes = [ln for ln in lanes if ln]
    tasks: dict[str, Any] = {
        "named": _safe(t(entities.match, text, scope, int(P["STEP_ENTITY_LIMIT"])), []),
        "entity": _safe(t(_by_meaning, text, "entity", project, int(P["STEP_ENTITY_LIMIT"]), floor, model), []),
        "log": (_safe(t(_by_meaning, text, config.HOTLOG_SCOPE, project, k, floor, model), [])
                if not is_time else _safe(asyncio.sleep(0, []), [])),
        "recent": (_safe(t(recent_rows, int(P["STEP_RECENT_LIMIT"]), since, until), [])
                   if is_time else _safe(asyncio.sleep(0, []), [])),
    }
    for ln in lanes:
        tasks[f"know:{ln}"] = _safe(t(_by_meaning, text, "knowledge", ln, k, floor, model), [])
    if personal:
        tasks["mine"] = _safe(t(_by_meaning, text, config.PERSONAL_LESSON_SCOPE, personal, k, floor, model), [])
    if keyed:
        for ln in real_lanes:
            tasks[f"key:{ln}"] = _safe(t(_key_read, ln, body.action, body.context, body.entities,
                                         int(P["STEP_KEY_LIMIT"]), text, model), ([], ""))
    for ln in real_lanes:
        tasks[f"contexts:{ln}"] = _safe(t(_contexts, ln), [])
    names = list(tasks)
    vals = await asyncio.gather(*tasks.values())
    R = dict(zip(names, vals))

    # 1. entity
    named = R["named"] or []
    for card in named:
        pack.add("entity", {"id": card["id"], "score": None, "text": entities.line_of(card)})
    for h in R["entity"] or []:
        pack.add("entity", _row_item(h, text, item_chars))
    # 2. distilled
    keys_info = None
    if keyed:
        results = {ln: R.get(f"key:{ln}") or ([], "") for ln in real_lanes}
        for lane, item, key in _exact_first(results, body.action, body.context):
            pack.add("distilled", {"id": item.get("id"), "score": None, "key": key, "status": item.get("status"),
                                   "text": _line_of(item, with_sources=maint)})
        keys_info = {"action": body.action, "context": body.context, "entities": body.entities,
                     "answered_by": {ln: res[1] for ln, res in results.items() if res[1]}}
    for wi in body.with_items or []:
        pack.add("distilled", {"id": wi.id, "score": None, "key": "injected", "status": "candidate",
                               "injected": True, "text": wi.text})
    # 3. personal-lesson
    for h in R.get("mine") or []:
        it = _row_item(h, text, item_chars)
        it["scope"] = config.PERSONAL_LESSON_SCOPE
        it["project"] = personal
        pack.add("personal-lesson", it)
    # 4-5. agents / daily: capability tiers not shipped (KH_STEP_NS_ITEMS / KH_STEP_DAILY default 0)
    # 6. log-recent
    for h in R["recent"] or []:
        pack.add("log-recent", _row_item({**h, "score": h.get("score")}, text, item_chars))
    # 7. knowledge + log
    know = []
    for ln in lanes:
        know.extend(R.get(f"know:{ln}") or [])
    know = _withheld(know)
    logs = R["log"] or []
    log_skipped = 0
    best_know = max((h["score"] for h in know), default=0.0)
    joined = [("knowledge", h) for h in know]
    if body.include_log or best_know < float(P["STEP_KNOW_ENOUGH"]):
        joined += [("log", h) for h in logs]
    else:
        log_skipped = len(logs)
    joined.sort(key=lambda x: -x[1]["score"])
    relabel = bool(body.only_tiers and "distilled" in body.only_tiers and "knowledge" not in body.only_tiers)
    for tier, h in joined:
        if relabel and tier == "knowledge" and str(h["vector_id"]).startswith("dk_"):
            tier = "distilled"
        pack.add(tier, _row_item(h, text, item_chars))

    threads = await asyncio.to_thread(_open_threads, pack, text, scope, named, log_skipped, project, is_time,
                                      recent_rows, int(P["STEP_THREAD_LIMIT"]))
    tiers: list[str] = []
    for it in pack.items:
        if it["tier"] not in tiers:
            tiers.append(it["tier"])
    scores = [it["score"] for it in pack.items if isinstance(it.get("score"), (int, float))]
    out = {
        "project": project, "time_step": is_time, "since": since, "tiers": tiers, "floor": floor,
        "budget_chars": pack.budget, "used_chars": pack.used, "cut_at_budget": bool(pack.dropped),
        "count": len(pack.items), "best_score": max(scores) if scores else None, "items": pack.items,
        "open_threads": threads,
    }
    if keys_info is not None:
        out["keys"] = keys_info
    ctx: list[str] = []
    for ln in real_lanes:
        for c in R.get(f"contexts:{ln}") or []:
            if c and c not in ctx:
                ctx.append(c)
    if ctx:
        out["contexts"] = ctx[:int(P["STEP_CONTEXT_LIMIT"])]
    return out


def _open_threads(pack: _Pack, text: str, scope: list[str], named: list[dict], log_skipped: int,
                  project: Optional[str], is_time: bool, recent_rows, limit: int) -> list[dict]:
    from app import entities
    from app import hotlog

    threads: list[dict] = []
    packed_text = "\n".join(it.get("text") or "" for it in pack.items)
    try:
        targets = " ; ".join(r.get("target") or "" for c in named for r in c.get("relations") or []
                             if c["id"] in pack.ids)
        cards = entities.match(packed_text + " \n " + targets, scope, 10)
        missing = [c["name"] for c in cards if c["id"] not in pack.ids]
        if missing:
            threads.append({"kind": "entity-not-loaded", "names": missing[:limit], "hint": HINTS["entity-not-loaded"]})
    except Exception as e:  # noqa: BLE001
        log.debug("entity thread failed: %s", e)
    refs: list[str] = []
    for m in _REF.finditer(packed_text):
        r = m.group(0)
        if r not in refs and r not in text:
            refs.append(r)
    if refs:
        threads.append({"kind": "reference-not-looked-up", "refs": refs[:limit],
                        "hint": HINTS["reference-not-looked-up"]})
    if pack.dropped:
        threads.append({"kind": "cut-at-budget", "count": len(pack.dropped),
                        "ids": [d["id"] for d in pack.dropped][:limit], "hint": HINTS["cut-at-budget"]})
    if log_skipped:
        threads.append({"kind": "log-skipped", "count": log_skipped, "hint": HINTS["log-skipped"]})
    log_items = [it for it in pack.items if it["tier"] == "log"]
    if project and not is_time and log_items:
        try:
            rows = recent_rows(1, None, None)
            if rows:
                top = rows[0]
                ts = (top.get("metadata") or {}).get("ts")
                packed_max = max((hotlog.parse_ts(it.get("ts")) for it in log_items), default=0.0)
                if top["vector_id"] not in pack.ids and hotlog.parse_ts(ts) > packed_max:
                    threads.append({"kind": "newer-log", "ts": ts, "id": top["vector_id"], "hint": HINTS["newer-log"]})
        except Exception as e:  # noqa: BLE001
            log.debug("newer-log thread failed: %s", e)
    return threads


def attach(app: FastAPI, recent: Optional[Callable[..., dict]] = None) -> None:
    @app.post("/context/step", tags=["context"])
    async def context_step(body: StepRequest, request: Request):
        rec = recent or getattr(request.app.state, "log_recent", None)
        return await build(body, U.model_of(request), rec)
