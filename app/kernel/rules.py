"""Kernel rule table (spec §3.8.2): tiers, slots, locked rules, guards; check/add contract, resolve, render, parts."""
from __future__ import annotations

import difflib
import json
import logging
import os
from typing import Any, Optional

from app import sqldb
from app import ctx_config as C
from app import ctx_util as U
from app.kernel import tags as T

log = logging.getLogger(__name__)

COMMON = "common"
TIER_RANK = {"common": 0, "project": 1, "personal": 2}
SAME_SCORE = C.KERNEL_SAME_SCORE
CONTENT_MAX = C.KERNEL_CONTENT_MAX
TITLE_MAX = 200
GUARD_MAX = 2000
OWN_BLOCK_HEADER = "PERSONAL AND PROJECT RULES (already merged with the rules above; on overlap they win):"
LEGACY_BLOCK_HEADER = ("PERSONALIZATION NOT YET TAGGED (wins over the common rules on overlap, except the locked "
                       "ones):")

_SQLITE = """CREATE TABLE IF NOT EXISTS kernel_rule (
    id TEXT PRIMARY KEY, tier TEXT NOT NULL, slot TEXT NOT NULL, title TEXT, content TEXT, tags TEXT,
    position INTEGER DEFAULT 0, locked INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1, overrides TEXT DEFAULT '',
    reason TEXT DEFAULT '', updated_by TEXT, updated_at TEXT, UNIQUE(tier, slot))"""
_HANA = """CREATE COLUMN TABLE kernel_rule (
    id NVARCHAR(32) PRIMARY KEY, tier NVARCHAR(200) NOT NULL, slot NVARCHAR(64) NOT NULL, title NVARCHAR(400),
    content NCLOB, tags NVARCHAR(2000), position INTEGER DEFAULT 0, locked INTEGER DEFAULT 0,
    enabled INTEGER DEFAULT 1, overrides NVARCHAR(64) DEFAULT '', reason NVARCHAR(2000) DEFAULT '',
    updated_by NVARCHAR(320), updated_at NVARCHAR(40), UNIQUE(tier, slot))"""
_COLS = ("id", "tier", "slot", "title", "content", "tags", "position", "locked", "enabled", "overrides", "reason",
         "updated_by", "updated_at", "guard")
_ready = False


class KernelError(Exception):
    def __init__(self, status: int, error: str, detail: str, **extra: Any):
        super().__init__(detail)
        self.status = status
        self.payload = {"error": error, "detail": detail, **extra}


def ensure() -> None:
    global _ready
    if _ready:
        return
    with sqldb.connect() as c:
        sqldb.create_table(c, "kernel_rule", _SQLITE, _HANA)
        sqldb.add_column(c, "kernel_rule", "guard", "TEXT DEFAULT ''", "NVARCHAR(2000) DEFAULT ''")
        sqldb.create_index(c, "ix_kernel_rule_tier", "kernel_rule", ["tier", "enabled"])
    _ready = True


def tier_kind(tier: str | None) -> str:
    if not tier or tier == COMMON:
        return "common"
    if str(tier).startswith(_prefix()):
        return "personal"
    return "project"


def _prefix() -> str:
    from app import config

    return config.PERSONAL_PREFIX


def _row(r) -> dict:
    d = {k: r[i] for i, k in enumerate(_COLS)}
    d["tags"] = U.jload(d["tags"], [])
    d["position"] = int(d["position"] or 0)
    d["locked"] = bool(d["locked"])
    d["enabled"] = bool(d["enabled"])
    d["overrides"] = d["overrides"] or ""
    d["reason"] = d["reason"] or ""
    g = U.jload(d["guard"], None)
    d["guard"] = g if isinstance(g, dict) and g else None
    d["kind"] = tier_kind(d["tier"])
    return d


def list_rules(tier: Optional[str] = None, include_disabled: bool = False) -> list[dict]:
    ensure()
    sql = f"SELECT {', '.join(_COLS)} FROM kernel_rule"
    conds, params = [], []
    if tier:
        conds.append("tier=?")
        params.append(tier)
    if not include_disabled:
        conds.append("enabled=1")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    with sqldb.connect() as c:
        rows = [_row(r) for r in c.execute(sql, params).fetchall()]
    rows.sort(key=lambda r: (r["tier"], r["position"], r["slot"]))
    return rows


def get(tier: str, slot: str) -> Optional[dict]:
    ensure()
    with sqldb.connect() as c:
        r = c.execute(f"SELECT {', '.join(_COLS)} FROM kernel_rule WHERE tier=? AND slot=?", (tier, slot)).fetchone()
    return _row(r) if r else None


def get_by_id(rid: str) -> Optional[dict]:
    ensure()
    with sqldb.connect() as c:
        r = c.execute(f"SELECT {', '.join(_COLS)} FROM kernel_rule WHERE id=?", (rid,)).fetchone()
    return _row(r) if r else None


def count(tier: str = COMMON) -> int:
    ensure()
    with sqldb.connect() as c:
        return int(c.execute("SELECT COUNT(*) FROM kernel_rule WHERE tier=?", (tier,)).fetchone()[0])


def delete(rid: str) -> int:
    ensure()
    with sqldb.connect() as c:
        return max(0, c.execute("DELETE FROM kernel_rule WHERE id=?", (rid,)).rowcount or 0)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_tags(raw) -> list[str]:
    tags: list[str] = []
    for t in raw or []:
        t = str(t).strip().lower()
        if t and t not in tags:
            tags.append(t)
    if not (C.KERNEL_TAGS_MIN <= len(tags) <= C.KERNEL_TAGS_MAX):
        raise KernelError(422, "KERNEL-TAGS", f"give {C.KERNEL_TAGS_MIN}..{C.KERNEL_TAGS_MAX} tags; got {len(tags)}")
    act = T.active()
    unknown = [t for t in tags if t not in act]
    if unknown:
        closest = {t: difflib.get_close_matches(t, list(act), n=3, cutoff=0.5) for t in unknown}
        raise KernelError(422, "KERNEL-TAGS-UNKNOWN", "unknown or inactive tags: " + ", ".join(unknown),
                          unknown=unknown, closest=closest,
                          hint="pick tags from GET /kernel/tags/suggest?text=..., or propose a new one with "
                               "POST /kernel/tags/propose")
    if all(act[t]["facet"] in T.GENERIC_FACETS for t in tags):
        raise KernelError(422, "KERNEL-TAGS-WHAT",
                          "every tag says when or where (facets moment, cli); add at least one tag saying what the "
                          "rule is about")
    return tags


def _str(v) -> str:
    if isinstance(v, bool):
        return str(v).lower()
    if v is None:
        return ""
    return str(v)


def validate_guard(raw) -> Optional[dict]:
    if raw in (None, "", {}):
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            raise KernelError(422, "KERNEL-GUARD", "guard must be a JSON object")
    if not isinstance(raw, dict):
        raise KernelError(422, "KERNEL-GUARD", "guard must be an object {tool, decision, when}")
    tool = str(raw.get("tool") or "").strip()
    if not tool or len(tool) > 120:
        raise KernelError(422, "KERNEL-GUARD", "guard.tool is required (at most 120 chars)")
    decision = str(raw.get("decision") or "ask").strip().lower()
    if decision not in ("ask", "deny"):
        raise KernelError(422, "KERNEL-GUARD", "guard.decision must be ask or deny")
    when = raw.get("when") or {}
    if not isinstance(when, dict):
        raise KernelError(422, "KERNEL-GUARD", "guard.when must be {field: [values]}")
    w2: dict[str, list[str]] = {}
    for k, vals in when.items():
        if not isinstance(vals, list):
            vals = [vals]
        w2[str(k)] = [_str(v) for v in vals]
    extra = set(raw) - {"tool", "decision", "when"}
    if extra:
        raise KernelError(422, "KERNEL-GUARD", "unknown guard fields: " + ", ".join(sorted(extra)))
    g = {"tool": tool, "decision": decision, "when": w2}
    if len(json.dumps(g)) > GUARD_MAX:
        raise KernelError(422, "KERNEL-GUARD", f"guard JSON is over {GUARD_MAX} chars")
    return g


def core(tags_: list[str]) -> set[str]:
    rows = T.all_rows()
    return {t for t in tags_ if (rows.get(t) or {}).get("facet") not in T.GENERIC_FACETS}


def similarity(tags_a: list[str], content_a: str, tags_b: list[str], content_b: str) -> float:
    a, b = core(tags_a), core(tags_b)
    inter = len(a & b)
    contain = inter / min(len(a), len(b)) if a and b else 0.0
    jac = inter / len(a | b) if (a | b) else 0.0
    wa, wb = set(T.words(content_a)), set(T.words(content_b))
    w = len(wa & wb) / min(len(wa), len(wb)) if wa and wb else 0.0
    return round(0.6 * contain + 0.15 * jac + 0.25 * w, 3)


def new_id() -> str:
    return "kr_" + os.urandom(6).hex()


def new_slot() -> str:
    return "sl_" + os.urandom(5).hex()


def check(body: dict, maintainer: bool = False, actor: Optional[str] = None) -> dict:
    """Dry run of the add contract; raises KernelError. Returns the plan that `add` would store."""
    tier = str(body.get("tier") or "").strip()
    if not tier:
        raise KernelError(400, "KERNEL-TIER", "tier is required (common, a project id, or personal)")
    content = U.collapse(body.get("content"))
    if not content:
        raise KernelError(400, "KERNEL-CONTENT", "content is required")
    if len(content) > CONTENT_MAX:
        raise KernelError(422, "KERNEL-TOO-LONG", f"content is {len(content)} chars; the limit is {CONTENT_MAX}")
    title = (U.collapse(body.get("title")) or content[:60])[:TITLE_MAX]
    tags_ = validate_tags(body.get("tags"))
    guard = validate_guard(body.get("guard"))
    notes: list[str] = []
    slot = str(body.get("slot") or "").strip() or None
    overrides = str(body.get("overrides") or "").strip() or None
    distinct = [str(s) for s in (body.get("distinct_from") or [])]
    reason = str(body.get("reason") or "").strip()
    kind = tier_kind(tier)
    own: Optional[dict] = None
    common_base: Optional[dict] = None

    if slot and tier != COMMON:
        own = get(tier, slot)
        if own is None:
            raise KernelError(404, "KERNEL-NO-SLOT", f"tier '{tier}' has no rule with slot '{slot}'")
        if own["overrides"]:
            if overrides and overrides != own["overrides"]:
                raise KernelError(422, "KERNEL-OVERRIDES",
                                  f"this rule overrides '{own['overrides']}'; that cannot change")
            overrides = own["overrides"]
        elif overrides:
            raise KernelError(422, "KERNEL-OVERRIDES", "an own-slot rule cannot become an override; add a new rule")
    elif slot and tier == COMMON:
        own = get(COMMON, slot)

    if overrides:
        if tier == COMMON:
            raise KernelError(422, "KERNEL-OVERRIDES", "a common rule cannot override another common rule")
        common_base = get(COMMON, overrides)
        if common_base is None:
            raise KernelError(404, "KERNEL-NO-SLOT", f"the common tier has no rule with slot '{overrides}'")
        if common_base["locked"]:
            raise KernelError(409, "KERNEL-LOCKED", f"the common rule '{overrides}' is locked; it cannot be replaced")
        if list(tags_) != list(common_base["tags"]):
            notes.append(f"tags were replaced by the tags of the common rule '{overrides}'")
        tags_ = list(common_base["tags"])
        slot = overrides
    if not overrides and own is None:
        if distinct and not reason:
            raise KernelError(422, "KERNEL-REASON", "distinct_from needs a reason")
        others = [r for r in list_rules(COMMON) if r["slot"] != slot]
        cands = []
        for r in others:
            s = similarity(tags_, content, r["tags"], r["content"] or "")
            if s >= SAME_SCORE and r["slot"] not in distinct:
                cands.append({"slot": r["slot"], "title": r["title"], "tags": r["tags"], "locked": r["locked"],
                              "score": s})
        if cands:
            cands.sort(key=lambda x: -x["score"])
            raise KernelError(409, "KERNEL-SAME-AS-COMMON", "this rule looks like an existing common rule",
                              candidates=cands[:5],
                              hint="resend with overrides=<slot> (it replaces that rule, tags are copied from it), "
                                   "or distinct_from=[slots] and a reason if it is a different rule")

    try:
        top = [s["tag"] for s in T.suggest(f"{title} {content}", limit=6)]
        missing = [t for t in top if t not in tags_]
        if missing:
            notes.append("suggested tags not used: " + ", ".join(missing))
    except Exception:  # noqa: BLE001
        pass

    locked = bool(body.get("locked"))
    if locked and tier != COMMON:
        notes.append("locked applies to tier common only; ignored")
        locked = False
    if body.get("position") is not None:
        position = int(body["position"])
    elif own is not None:
        position = own["position"]
    elif common_base is not None:
        position = common_base["position"]
    else:
        position = 1000
    return {
        "tier": tier, "kind": kind, "slot": slot or new_slot(), "title": title, "content": content, "tags": tags_,
        "position": position, "locked": locked if tier == COMMON else False,
        "enabled": bool(body.get("enabled", True)), "overrides": overrides or "", "reason": reason,
        "guard": guard, "updated_by": actor, "notes": notes,
    }


def store_plan(p: dict) -> dict:
    ensure()
    old = get(p["tier"], p["slot"])
    rid = old["id"] if old else new_id()
    row = {"id": rid, "tier": p["tier"], "slot": p["slot"], "title": p["title"], "content": p["content"],
           "tags": json.dumps(p["tags"]), "position": int(p["position"]), "locked": 1 if p["locked"] else 0,
           "enabled": 1 if p["enabled"] else 0, "overrides": p["overrides"] or "", "reason": p["reason"] or "",
           "updated_by": p.get("updated_by"), "updated_at": U.now_iso(),
           "guard": json.dumps(p["guard"]) if p.get("guard") else ""}
    with sqldb.connect() as c:
        sqldb.upsert(c, "kernel_rule", ["tier", "slot"], row)
    return {**{k: v for k, v in p.items()}, "id": rid, "result": "updated" if old else "added"}


def add(body: dict, maintainer: bool = False, actor: Optional[str] = None) -> dict:
    return store_plan(check(body, maintainer, actor))


# ---------------------------------------------------------------------------
# Resolve / render / parts / guards
# ---------------------------------------------------------------------------


def resolve(project: Optional[str] = None, personal: Optional[str] = None) -> list[dict]:
    tiers = [COMMON]
    if project and project != COMMON:
        tiers.append(project)
    if personal and personal not in tiers:
        tiers.append(personal)
    ensure()
    ph = ",".join("?" for _ in tiers)
    with sqldb.connect() as c:
        rows = [_row(r) for r in c.execute(
            f"SELECT {', '.join(_COLS)} FROM kernel_rule WHERE enabled=1 AND tier IN ({ph})", tiers).fetchall()]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["slot"], []).append(r)
    out = []
    for slot, grp in groups.items():
        grp.sort(key=lambda r: TIER_RANK[r["kind"]])
        base = grp[0] if grp[0]["kind"] == "common" else None
        if base is not None and base["locked"]:
            winner, replaced, blocked = base, [], grp[1:]
        else:
            winner, replaced, blocked = grp[-1], grp[:-1], []
        key = (base["position"] if base is not None else 1000 + winner["position"], TIER_RANK[winner["kind"]], slot)
        out.append((key, {"slot": slot, "tier": winner["tier"], "kind": winner["kind"], "rule": winner,
                          "replaced": [{"id": r["id"], "tier": r["tier"]} for r in replaced],
                          "blocked": [{"id": r["id"], "tier": r["tier"]} for r in blocked],
                          "_base": base is not None}))
    out.sort(key=lambda x: x[0])
    return [e for _k, e in out]


def render(project: Optional[str] = None, personal: Optional[str] = None,
           legacy_rows: Optional[list[str]] = None) -> list[str]:
    blocks: list[str] = []
    own: list[str] = []
    for e in resolve(project, personal):
        r = e["rule"]
        if e["kind"] == "common":
            blocks.append(r["content"] or "")
        elif r["overrides"] or e["_base"]:
            blocks.append(f"[{e['kind']}, replaces the common rule '{e['slot']}'] {r['content']}")
        else:
            own.append(r["content"] or "")
    if own:
        blocks.append(OWN_BLOCK_HEADER + "\n" + "\n".join(f"- {c}" for c in own))
    if legacy_rows:
        blocks.append(LEGACY_BLOCK_HEADER + "\n" + "\n".join(f"- {c}" for c in legacy_rows))
    return blocks


def parts(blocks: list[str], max_chars: int = C.KERNEL_PART_MAX) -> list[str]:
    out: list[str] = []
    cur = ""
    for b in blocks:
        if not cur:
            cur = b
        elif len(cur) + 2 + len(b) <= max_chars:
            cur = cur + "\n\n" + b
        else:
            out.append(cur)
            cur = b
    if cur:
        out.append(cur)
    return out


def guards(tool: str, project: Optional[str] = None, personal: Optional[str] = None) -> list[dict]:
    out = []
    for e in resolve(project, personal):
        g = e["rule"].get("guard")
        if g and g.get("tool") == tool:
            out.append({"id": e["rule"]["id"], "text": e["rule"]["content"], "decision": g.get("decision", "ask"),
                        "when": g.get("when") or {}, "tier": e["tier"], "slot": e["slot"]})
    return out


def insert_seed_row(tier: str, slot: str, title: str, content: str, tags_: list[str], position: int, locked: bool,
                    reason: str = "") -> bool:
    """Insert one seed row; never overwrites an existing (tier, slot)."""
    ensure()
    with sqldb.connect() as c:
        return sqldb.insert_new(c, "kernel_rule", {
            "id": new_id(), "tier": tier, "slot": slot, "title": title, "content": content, "tags": json.dumps(tags_),
            "position": int(position), "locked": 1 if locked else 0, "enabled": 1, "overrides": "",
            "reason": reason, "updated_by": "seed", "updated_at": U.now_iso(), "guard": ""})
