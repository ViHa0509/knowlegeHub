"""rules (spec §6.7): a rule graph of (entity, attribute, condition, effect, source quote, validity).

KH stores, links and walks rules; it never judges them. SQL (`kh_rule`, `kh_rule_link`) is the system of record;
a per-(ns, project) graph projection (`graph_db.get_project_graph("<ns>__<project>")`) is written after commit and
only speeds up `rules:query`. Conflict candidates, coverage scans and a run gate are computed here.
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from typing import Any

from fastapi import Depends, FastAPI, Query, Request

from app import sqldb
from app.caps import core
from app.caps.core import CapError, Ctx

log = logging.getLogger(__name__)

NAME = "rules"
VERSION = "1.0.0"
STATUS = "beta"
DEFAULT_ON = True
BULK_MAX = 5000
HOPS_MAX = 3
START_MAX = 100
QUERY_RULES_MAX = 5000
SCAN_ENTITIES_MAX = 10000
CONFLICTS_MAX = 500
LINKS_MAX = 50
LIST_MAX = 1000
SCANS_LIST_MAX = 500
SCAN_LIST_CAP = 1000
SOURCE_TEXT_MAX = 16000
PROVENANCE_MAX = 16 * 1024
TEXT_MAX = 4000
NAME_MAX = 512
REF_MAX = 256
REPROJECT_BATCH = 20
REPROJECT_INTERVAL_S = 300
LIMITS = {"bulk": BULK_MAX, "hops": HOPS_MAX, "start_entities": START_MAX, "query_rules": QUERY_RULES_MAX,
          "scan_entities": SCAN_ENTITIES_MAX, "conflicts": CONFLICTS_MAX, "links_per_rule": LINKS_MAX,
          "list": LIST_MAX, "source_text_chars": SOURCE_TEXT_MAX, "provenance_bytes": PROVENANCE_MAX}
TABLES = ["kh_rule", "kh_rule_link", "kh_rule_run", "kh_rule_scan"]
LOCAL_TABLES = ["kh_rule_proj"]

ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
VALIDITY = ("unverified", "verified", "contradicted", "superseded", "retired")
DEAD = ("superseded", "retired")
LIVE = tuple(v for v in VALIDITY if v not in DEAD)
CONFIRMED = ("verified", "contradicted")
ORIGINS = ("bootstrap", "scan", "write_back")
DEFAULT_ORIGIN = "write_back"
RELATIONS = ("APPLIES_TO", "AFFECTS", "DEPENDS_ON")
LINK_RELATIONS = ("AFFECTS", "DEPENDS_ON")
RULE_KEYS = {"rule_id", "project", "entity", "attribute", "condition", "effect", "quote_id", "source_text",
             "validity", "origin", "confidence", "provenance", "links", "superseded_by", "run_id"}


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _m1(c) -> None:
    sqldb.create_table(
        c, "kh_rule",
        "CREATE TABLE IF NOT EXISTS kh_rule (ns TEXT NOT NULL, rule_id TEXT NOT NULL, project TEXT, entity TEXT, "
        "entity_key TEXT, attribute TEXT, attribute_key TEXT, cond TEXT, effect TEXT, quote_id TEXT, "
        "source_text TEXT, validity TEXT NOT NULL, confidence REAL, origin TEXT, run_id TEXT, superseded_by TEXT, "
        "rule_hash TEXT NOT NULL, provenance_json TEXT, actor_json TEXT, rev INTEGER NOT NULL DEFAULT 1, "
        "created_ms INTEGER, updated_ms INTEGER, PRIMARY KEY (ns, rule_id))",
        "CREATE COLUMN TABLE kh_rule (ns NVARCHAR(64) NOT NULL, rule_id NVARCHAR(128) NOT NULL, "
        "project NVARCHAR(256), entity NVARCHAR(512), entity_key NVARCHAR(512), attribute NVARCHAR(512), "
        "attribute_key NVARCHAR(512), cond NCLOB, effect NCLOB, quote_id NVARCHAR(256), source_text NCLOB, "
        "validity NVARCHAR(16) NOT NULL, confidence DOUBLE, origin NVARCHAR(16), run_id NVARCHAR(256), "
        "superseded_by NVARCHAR(128), rule_hash NVARCHAR(64) NOT NULL, provenance_json NCLOB, actor_json NCLOB, "
        "rev BIGINT NOT NULL, created_ms BIGINT, updated_ms BIGINT, PRIMARY KEY (ns, rule_id))")
    sqldb.create_index(c, "kh_rule_hash", "kh_rule", ["ns", "rule_hash"], unique=True)
    sqldb.create_index(c, "kh_rule_ent", "kh_rule", ["ns", "project", "entity_key"])
    sqldb.create_table(
        c, "kh_rule_link",
        "CREATE TABLE IF NOT EXISTS kh_rule_link (ns TEXT NOT NULL, rule_id TEXT NOT NULL, project TEXT, "
        "relation TEXT NOT NULL, to_entity TEXT, to_key TEXT NOT NULL, PRIMARY KEY (ns, rule_id, relation, to_key))",
        "CREATE COLUMN TABLE kh_rule_link (ns NVARCHAR(64) NOT NULL, rule_id NVARCHAR(128) NOT NULL, "
        "project NVARCHAR(256), relation NVARCHAR(16) NOT NULL, to_entity NVARCHAR(512), "
        "to_key NVARCHAR(512) NOT NULL, PRIMARY KEY (ns, rule_id, relation, to_key))")
    sqldb.create_index(c, "kh_rule_link_to", "kh_rule_link", ["ns", "project", "to_key"])
    sqldb.create_table(
        c, "kh_rule_run",
        "CREATE TABLE IF NOT EXISTS kh_rule_run (ns TEXT NOT NULL, run_id TEXT NOT NULL, rule_id TEXT NOT NULL, "
        "action TEXT NOT NULL, at_ms INTEGER, PRIMARY KEY (ns, run_id, rule_id, action))",
        "CREATE COLUMN TABLE kh_rule_run (ns NVARCHAR(64) NOT NULL, run_id NVARCHAR(256) NOT NULL, "
        "rule_id NVARCHAR(128) NOT NULL, action NVARCHAR(16) NOT NULL, at_ms BIGINT, "
        "PRIMARY KEY (ns, run_id, rule_id, action))")
    sqldb.create_table(
        c, "kh_rule_scan",
        "CREATE TABLE IF NOT EXISTS kh_rule_scan (ns TEXT NOT NULL, scan_id TEXT NOT NULL, project TEXT, "
        "run_id TEXT, request_sha TEXT, n_scanned INTEGER, n_any INTEGER, n_verified INTEGER, coverage_any REAL, "
        "coverage_verified REAL, result_json TEXT, actor_json TEXT, created_ms INTEGER, PRIMARY KEY (ns, scan_id))",
        "CREATE COLUMN TABLE kh_rule_scan (ns NVARCHAR(64) NOT NULL, scan_id NVARCHAR(128) NOT NULL, "
        "project NVARCHAR(256), run_id NVARCHAR(256), request_sha NVARCHAR(64), n_scanned BIGINT, n_any BIGINT, "
        "n_verified BIGINT, coverage_any DOUBLE, coverage_verified DOUBLE, result_json NCLOB, actor_json NCLOB, "
        "created_ms BIGINT, PRIMARY KEY (ns, scan_id))")
    sqldb.create_table(
        c, "kh_rule_proj",
        "CREATE TABLE IF NOT EXISTS kh_rule_proj (ns TEXT NOT NULL, project TEXT NOT NULL, state TEXT NOT NULL, "
        "error TEXT, updated_ms INTEGER, PRIMARY KEY (ns, project))",
        "CREATE COLUMN TABLE kh_rule_proj (ns NVARCHAR(64) NOT NULL, project NVARCHAR(256) NOT NULL, "
        "state NVARCHAR(16) NOT NULL, error NVARCHAR(2000), updated_ms BIGINT, PRIMARY KEY (ns, project))")


def init(c) -> None:
    sqldb.migrate(c, NAME, [(1, _m1)])


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def fold(s: Any) -> str:
    """NFD, strip accents, lowercase, runs of non-alphanumerics -> one space."""
    t = unicodedata.normalize("NFD", str(s or ""))
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn").lower()
    return re.sub(r"[\W_]+", " ", t).strip()


def squash(s: Any) -> str:
    return " ".join(str(s or "").lower().split())


def rule_hash(project: str, entity_key: str, attribute_key: str, cond: str, effect: str) -> str:
    return core.sha256_text(json.dumps([project or "", entity_key, attribute_key, squash(cond), squash(effect)],
                                       ensure_ascii=False, separators=(",", ":")))


def node_ent(key: str) -> str:
    return "ent__" + key.replace(" ", "-")


def node_rule(rid: str) -> str:
    return "rule__" + rid


# ---------------------------------------------------------------------------
# Conflict candidates
# ---------------------------------------------------------------------------

_SPLIT = re.compile(r"\s+and\s+|&&|;|,")
UNCONDITIONAL = {"", "*", "always", "true", "any"}
_NUM_OPS = [
    (re.compile(r"^(.+?)\s+(?:is\s+)?(?:greater than or equal to|at least)\s+(-?\d+(?:\.\d+)?)\s*\S*$"), ">="),
    (re.compile(r"^(.+?)\s+(?:is\s+)?(?:less than or equal to|at most)\s+(-?\d+(?:\.\d+)?)\s*\S*$"), "<="),
    (re.compile(r"^(.+?)\s+(?:is\s+)?(?:greater than|above|over|exceeds|more than)\s+(-?\d+(?:\.\d+)?)\s*\S*$"),
     ">"),
    (re.compile(r"^(.+?)\s+(?:is\s+)?(?:less than|below|under|fewer than)\s+(-?\d+(?:\.\d+)?)\s*\S*$"), "<"),
    (re.compile(r"^(.+?)\s*(>=|<=|>|<|==|=)\s*(-?\d+(?:\.\d+)?)\s*\S*$"), None),
]


def clauses(cond: Any) -> list[str]:
    s = squash(cond)
    if s in UNCONDITIONAL:
        return []
    return sorted({x.strip() for x in _SPLIT.split(s) if x.strip()})


def _numeric(cl: str) -> tuple[str, str, float] | None:
    for rx, op in _NUM_OPS:
        m = rx.match(cl)
        if m:
            if op is None:
                return m.group(1).strip(), ("=" if m.group(2) in ("=", "==") else m.group(2)), float(m.group(3))
            return m.group(1).strip(), op, float(m.group(2))
    return None


def _interval(op: str, v: float) -> tuple[float, bool, float, bool]:
    inf = float("inf")
    return {">": (v, False, inf, False), ">=": (v, True, inf, False), "<": (-inf, False, v, False),
            "<=": (-inf, False, v, True), "=": (v, True, v, True)}[op]


def _disjoint(a: tuple, b: tuple) -> bool:
    lo = max(a[0], b[0])
    hi = min(a[2], b[2])
    if lo < hi:
        return False
    if lo > hi:
        return True
    lo_in = (a[1] if a[0] == lo else True) and (b[1] if b[0] == lo else True)
    hi_in = (a[3] if a[2] == hi else True) and (b[3] if b[2] == hi else True)
    return not (lo_in and hi_in)


def exclusive(a: str, b: str) -> bool:
    """Two clauses that can never hold together (provably)."""
    na, nb = _numeric(a), _numeric(b)
    if na and nb and na[0] == nb[0]:
        return _disjoint(_interval(na[1], na[2]), _interval(nb[1], nb[2]))
    for x, y in ((a, b), (b, a)):
        if x.startswith("not ") and x[4:].strip() == y:
            return True
        m = re.match(r"^(.+?) is not (.+)$", x)
        if m and y == f"{m.group(1)} is {m.group(2)}":
            return True
    ea, eb = re.match(r"^(.+?)\s*==?\s*(.+)$", a), re.match(r"^(.+?)\s*==?\s*(.+)$", b)
    if ea and eb and ea.group(1).strip() == eb.group(1).strip() and ea.group(2).strip() != eb.group(2).strip():
        return True
    return False


def overlap(cond_a: Any, cond_b: Any) -> str | None:
    """unconditional | same | subsumes | possible, or None when the conditions are provably exclusive."""
    a, b = clauses(cond_a), clauses(cond_b)
    if not a or not b:
        return "unconditional"
    sa, sb = set(a), set(b)
    if sa == sb:
        return "same"
    if sa <= sb or sb <= sa:
        return "subsumes"
    for x in a:
        for y in b:
            if exclusive(x, y):
                return None
    return "possible"


def conflicts(rules: list[dict], limit: int = CONFLICTS_MAX) -> tuple[list[dict], bool]:
    groups: dict[tuple, list[dict]] = {}
    for r in rules:
        if r["validity"] in DEAD:
            continue
        groups.setdefault((r["entity_key"], r["attribute_key"]), []).append(r)
    out: list[dict] = []
    for (ek, ak), rs in groups.items():
        rs = sorted(rs, key=lambda r: r["rule_id"])
        for i in range(len(rs)):
            for j in range(i + 1, len(rs)):
                a, b = rs[i], rs[j]
                if squash(a["effect"]) == squash(b["effect"]):
                    continue
                kind = overlap(a["condition"], b["condition"])
                if kind is None:
                    continue
                if len(out) >= limit:
                    return out, True
                out.append({"rule_ids": [a["rule_id"], b["rule_id"]], "entity_key": ek, "attribute_key": ak,
                            "overlap": kind})
    return out, False


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def policy(ctx: Ctx) -> dict:
    s = ctx.settings("rules")
    return s if isinstance(s, dict) else {}


def _require(ctx: Ctx, op: str) -> dict:
    ctx.require("rules", op)
    pol = policy(ctx)
    if not core.acl_allows(ctx.role, pol.get("read_roles" if op == "read" else "write_roles")):
        raise CapError("forbidden", f"role '{ctx.role}' may not {op} rules in this namespace")
    return pol


def can_confirm(ctx: Ctx) -> bool:
    if ctx.is_admin:
        return True
    cp = policy(ctx).get("confirm_policy") or {}
    roles, emails = cp.get("roles") or [], [str(e).lower() for e in (cp.get("emails") or [])]
    if roles or emails:
        return ctx.role in roles or (ctx.caller.email or "").lower() in emails
    return ctx.allows("rules", "confirm")


def graph_on() -> bool:
    return (os.environ.get("KH_RULES_GRAPH") or "true").strip().lower() not in ("false", "0", "off", "no")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _str(raw: dict, key: str, errs: list, maxlen: int, required: bool = False) -> str | None:
    v = raw.get(key)
    if v is None or v == "":
        if required:
            errs.append({"pointer": f"/{key}", "message": f"{key} is required"})
        return None if v is None else ""
    if not isinstance(v, str):
        errs.append({"pointer": f"/{key}", "message": f"{key} must be a string"})
        return None
    if len(v) > maxlen:
        errs.append({"pointer": f"/{key}", "message": f"{key} is longer than {maxlen} characters"})
        return None
    return v


def normalize(ctx: Ctx, raw: Any, project: str | None = None, run_id: str | None = None) -> tuple[dict | None,
                                                                                             list[dict]]:
    """Validate one rule body -> (rule, errors). Body filters must already have run."""
    if not isinstance(raw, dict):
        return None, [{"pointer": "/", "message": "a rule must be an object"}]
    errs: list[dict] = []
    for k in sorted(set(raw) - RULE_KEYS):
        errs.append({"pointer": f"/{k}", "message": "unknown key"})
    proj = raw.get("project", project)
    if proj is not None and not isinstance(proj, str):
        errs.append({"pointer": "/project", "message": "project must be a string"})
        proj = None
    try:
        proj = ctx.require_project(proj)
    except CapError as e:
        errs.append({"pointer": "/project", "message": str(e.detail)})
    entity = _str(raw, "entity", errs, NAME_MAX, True)
    attribute = _str(raw, "attribute", errs, NAME_MAX, True)
    cond = _str(raw, "condition", errs, TEXT_MAX) or ""
    effect = _str(raw, "effect", errs, TEXT_MAX, True)
    ek, ak = fold(entity), fold(attribute)
    if entity and not ek:
        errs.append({"pointer": "/entity", "message": "entity has no letters or digits"})
    if attribute and not ak:
        errs.append({"pointer": "/attribute", "message": "attribute has no letters or digits"})
    quote_id = _str(raw, "quote_id", errs, REF_MAX)
    source_text = _str(raw, "source_text", errs, SOURCE_TEXT_MAX)
    validity = raw.get("validity")
    if validity is not None and validity not in VALIDITY:
        errs.append({"pointer": "/validity", "message": f"validity must be one of {', '.join(VALIDITY)}"})
    origin = raw.get("origin") or DEFAULT_ORIGIN
    if origin not in ORIGINS:
        errs.append({"pointer": "/origin", "message": f"origin must be one of {', '.join(ORIGINS)}"})
    conf = raw.get("confidence")
    if conf is not None and (isinstance(conf, bool) or not isinstance(conf, (int, float)) or not 0 <= conf <= 1):
        errs.append({"pointer": "/confidence", "message": "confidence must be a number in [0, 1]"})
    prov = raw.get("provenance")
    if prov is not None and core.json_size(prov) > PROVENANCE_MAX:
        errs.append({"pointer": "/provenance", "message": f"provenance is larger than {PROVENANCE_MAX} bytes"})
    sup = _str(raw, "superseded_by", errs, 128)
    if sup and validity != "superseded":
        errs.append({"pointer": "/superseded_by", "message": "superseded_by is only allowed with validity "
                                                              "superseded"})
    rid_run = raw.get("run_id", run_id if run_id is not None else ctx.run_id)
    if rid_run is not None and (not isinstance(rid_run, str) or len(rid_run) > REF_MAX):
        errs.append({"pointer": "/run_id", "message": f"run_id must be a string of at most {REF_MAX} characters"})
    links_raw = raw.get("links") or []
    links: list[dict] = []
    if not isinstance(links_raw, list):
        errs.append({"pointer": "/links", "message": "links must be a list"})
    elif len(links_raw) > LINKS_MAX:
        errs.append({"pointer": "/links", "message": f"at most {LINKS_MAX} links per rule"})
    else:
        seen = set()
        for i, ln in enumerate(links_raw):
            rel = ln.get("relation") if isinstance(ln, dict) else None
            to = ln.get("to_entity") if isinstance(ln, dict) else None
            if rel not in LINK_RELATIONS:
                errs.append({"pointer": f"/links/{i}/relation",
                             "message": f"relation must be one of {', '.join(LINK_RELATIONS)}"})
                continue
            if not isinstance(to, str) or not fold(to) or len(to) > NAME_MAX:
                errs.append({"pointer": f"/links/{i}/to_entity", "message": "to_entity must be an entity name"})
                continue
            if (rel, fold(to)) not in seen:
                seen.add((rel, fold(to)))
                links.append({"relation": rel, "to_entity": to, "to_key": fold(to)})
    rid = raw.get("rule_id")
    if rid is not None and (not isinstance(rid, str) or not ID_RE.match(rid)):
        errs.append({"pointer": "/rule_id", "message": "rule_id must be 1-128 characters of [A-Za-z0-9_.-]"})
    if errs:
        return None, errs[:core.MAX_SCHEMA_ERRORS]
    h = rule_hash(proj, ek, ak, cond, effect or "")
    return {"rule_id": rid, "project": proj, "entity": entity, "entity_key": ek, "attribute": attribute,
            "attribute_key": ak, "cond": cond, "effect": effect, "quote_id": quote_id or None,
            "source_text": source_text or None, "validity": validity, "confidence": conf, "origin": origin,
            "run_id": rid_run, "superseded_by": sup or None, "rule_hash": h, "provenance": prov,
            "links": links}, []


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _chunks(seq: list, n: int = 500):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _links_of(c, ns: str, ids: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {i: [] for i in ids}
    for part in _chunks(list(ids)):
        for r in c.execute(f"SELECT rule_id, relation, to_entity, to_key FROM kh_rule_link WHERE ns=? AND rule_id "
                           f"IN ({', '.join('?' for _ in part)}) ORDER BY rule_id, relation, to_key",
                           [ns, *part]).fetchall():
            out.setdefault(r[0], []).append({"relation": r[1], "to_entity": r[2], "to_key": r[3]})
    return out


def _view(r, links: list[dict]) -> dict:
    return {"rule_id": r["rule_id"], "project": r["project"] or "", "entity": r["entity"],
            "entity_key": r["entity_key"], "attribute": r["attribute"], "attribute_key": r["attribute_key"],
            "condition": r["cond"] or "", "effect": r["effect"], "quote_id": r["quote_id"],
            "source_text": r["source_text"], "validity": r["validity"], "confidence": r["confidence"],
            "origin": r["origin"], "run_id": r["run_id"], "superseded_by": r["superseded_by"],
            "rule_hash": r["rule_hash"], "provenance": core.loads(r["provenance_json"], None), "links": links,
            "actor": core.loads(r["actor_json"], None), "rev": int(r["rev"]), "created_ms": r["created_ms"],
            "updated_ms": r["updated_ms"]}


def _views(c, ns: str, rows) -> list[dict]:
    rows = list(rows)
    links = _links_of(c, ns, [r["rule_id"] for r in rows])
    return [_view(r, links.get(r["rule_id"], [])) for r in rows]


def _read(c, ns: str, rid: str):
    return c.execute("SELECT * FROM kh_rule WHERE ns=? AND rule_id=?", (ns, rid)).fetchone()


def _mark_run(c, ns: str, run_id: str | None, rid: str, action: str, now: int) -> None:
    if run_id:
        sqldb.insert_new(c, "kh_rule_run", {"ns": ns, "run_id": run_id, "rule_id": rid, "action": action,
                                            "at_ms": now})


def _set_links(c, ns: str, rid: str, project: str, links: list[dict]) -> None:
    c.execute("DELETE FROM kh_rule_link WHERE ns=? AND rule_id=?", (ns, rid))
    for ln in links:
        sqldb.insert_new(c, "kh_rule_link", {"ns": ns, "rule_id": rid, "project": project, **ln})


def _upsert(c, ctx: Ctx, rid: str, rule: dict, expected: int | None, confirm_ok: bool, now: int,
            bulk: bool = False) -> tuple[str, str]:
    """Write one normalized rule. Returns (outcome created|updated|unchanged|duplicate, rule_id)."""
    other = c.execute("SELECT rule_id FROM kh_rule WHERE ns=? AND rule_hash=?", (ctx.ns, rule["rule_hash"])).fetchone()
    if other is not None and other[0] != rid:
        if bulk:
            _mark_run(c, ctx.ns, rule["run_id"], other[0], "written", now)
            return "duplicate", other[0]
        raise CapError("duplicate", f"the same rule already exists as '{other[0]}'",
                       extra={"existing_rule_id": other[0]})
    cur = _read(c, ctx.ns, rid)
    want = rule["validity"]
    if cur is None:
        if expected is not None:
            raise CapError("not_found", f"rule '{rid}' not found")
        if want in CONFIRMED and not confirm_ok:
            raise CapError("forbidden", f"setting validity {want} needs the confirm right")
        row = {"ns": ctx.ns, "rule_id": rid, "project": rule["project"], "entity": rule["entity"],
               "entity_key": rule["entity_key"], "attribute": rule["attribute"],
               "attribute_key": rule["attribute_key"], "cond": rule["cond"], "effect": rule["effect"],
               "quote_id": rule["quote_id"], "source_text": rule["source_text"], "validity": want or "unverified",
               "confidence": rule["confidence"], "origin": rule["origin"], "run_id": rule["run_id"],
               "superseded_by": rule["superseded_by"], "rule_hash": rule["rule_hash"],
               "provenance_json": core.dumps(rule["provenance"]) if rule["provenance"] is not None else None,
               "actor_json": ctx.actor_json(), "rev": 1, "created_ms": now, "updated_ms": now}
        if not sqldb.insert_new(c, "kh_rule", row):
            raise core.write_conflict(ctx.ns, f"rule '{rid}'", None, expected)
        _set_links(c, ctx.ns, rid, rule["project"], rule["links"])
        _mark_run(c, ctx.ns, rule["run_id"], rid, "written", now)
        return "created", rid
    if (cur["project"] or "") != rule["project"]:
        raise CapError("invalid", "the project of a rule cannot change", extra={"project": cur["project"] or ""})
    changed = cur["rule_hash"] != rule["rule_hash"]
    if bulk and not changed:
        _mark_run(c, ctx.ns, rule["run_id"], rid, "written", now)
        return "duplicate", rid
    if want is None:
        validity = "unverified" if changed and cur["validity"] in CONFIRMED else cur["validity"]
    else:
        if want in CONFIRMED and (changed or cur["validity"] != want) and not confirm_ok:
            raise CapError("forbidden", f"setting validity {want} needs the confirm right")
        validity = want
    sets = {"entity": rule["entity"], "entity_key": rule["entity_key"], "attribute": rule["attribute"],
            "attribute_key": rule["attribute_key"], "cond": rule["cond"], "effect": rule["effect"],
            "quote_id": rule["quote_id"], "source_text": rule["source_text"], "validity": validity,
            "confidence": rule["confidence"], "origin": rule["origin"], "run_id": rule["run_id"] or cur["run_id"],
            "superseded_by": rule["superseded_by"], "rule_hash": rule["rule_hash"],
            "provenance_json": core.dumps(rule["provenance"]) if rule["provenance"] is not None else None,
            "actor_json": ctx.actor_json(), "updated_ms": now}
    core.cas_update(c, "kh_rule", {"ns": ctx.ns, "rule_id": rid}, sets, expected, ns=ctx.ns, what=f"rule '{rid}'")
    _set_links(c, ctx.ns, rid, rule["project"], rule["links"])
    _mark_run(c, ctx.ns, rule["run_id"], rid, "written", now)
    return "updated", rid


# ---------------------------------------------------------------------------
# Graph projection
# ---------------------------------------------------------------------------


def _graph(ns: str, project: str):
    from app import graph_db

    return graph_db.get_project_graph(f"{ns}__{project}")


def _proj_state(c, ns: str, project: str) -> str | None:
    r = c.execute("SELECT state FROM kh_rule_proj WHERE ns=? AND project=?", (ns, project)).fetchone()
    return r[0] if r else None


def _set_proj(ns: str, project: str, state: str, error: str | None = None) -> None:
    with sqldb.connect() as c:
        sqldb.upsert(c, "kh_rule_proj", ["ns", "project"], {"ns": ns, "project": project, "state": state,
                                                             "error": (error or None) and error[:2000],
                                                             "updated_ms": sqldb.now_ms(c)})


def _graph_write(g, rules: list[dict]) -> int:
    if not rules:
        return 0
    g.delete_nodes([node_rule(r["rule_id"]) for r in rules])
    nodes, edges, ents = [], [], {}
    for r in rules:
        nodes.append({"core_id": node_rule(r["rule_id"]), "type": "rule", "label": r["rule_id"],
                      "attributes": {"entity_key": r["entity_key"], "attribute_key": r["attribute_key"],
                                     "validity": r["validity"]}})
        ents[r["entity_key"]] = r["entity"]
        edges.append({"source_core_id": node_rule(r["rule_id"]), "relation": "APPLIES_TO",
                      "target_core_id": node_ent(r["entity_key"])})
        for ln in r["links"]:
            ents.setdefault(ln["to_key"], ln["to_entity"])
            edges.append({"source_core_id": node_rule(r["rule_id"]), "relation": ln["relation"],
                          "target_core_id": node_ent(ln["to_key"])})
    nodes += [{"core_id": node_ent(k), "type": "entity", "label": v, "attributes": {"key": k}}
              for k, v in ents.items()]
    g.upsert_nodes(nodes)
    g.upsert_edges(edges)
    return len(edges)


def project_after_commit(ns: str, project: str, rule_ids: list[str]) -> str:
    """Incremental projection of rules just written (never raises). Returns ok | stale | off."""
    if not graph_on():
        return "off"
    try:
        with sqldb.connect() as c:
            state = _proj_state(c, ns, project)
            if state in ("stale", "rebuilding"):
                if state == "rebuilding":
                    c.execute("UPDATE kh_rule_proj SET state='stale' WHERE ns=? AND project=?", (ns, project))
                return "stale"
            rows = []
            for part in _chunks(list(dict.fromkeys(rule_ids))):
                rows += c.execute(f"SELECT * FROM kh_rule WHERE ns=? AND rule_id IN "
                                  f"({', '.join('?' for _ in part)})", [ns, *part]).fetchall()
            rules = _views(c, ns, rows)
        _graph_write(_graph(ns, project), rules)
        if state is None:
            _set_proj(ns, project, "ok")
        return "ok"
    except Exception as e:  # noqa: BLE001
        log.warning("rules projection %s/%s failed: %s", ns, project, e)
        try:
            _set_proj(ns, project, "stale", str(e))
        except Exception:  # noqa: BLE001
            pass
        return "stale"


def reproject(ns: str, project: str) -> dict:
    """Rebuild the projection of one (ns, project) from SQL. Stays stale if rules moved meanwhile."""
    if not graph_on():
        return {"project": project, "state": "off", "rules": 0, "edges": 0}
    from app import graph_db

    with sqldb.connect() as c:
        start = sqldb.now_ms(c)
        sqldb.upsert(c, "kh_rule_proj", ["ns", "project"], {"ns": ns, "project": project, "state": "rebuilding",
                                                             "error": None, "updated_ms": start})
    try:
        with sqldb.connect() as c:
            rules = _views(c, ns, c.execute("SELECT * FROM kh_rule WHERE ns=? AND project=? ORDER BY rule_id",
                                            (ns, project)).fetchall())
        graph_db.drop_project_graph(f"{ns}__{project}")
        n_edges = _graph_write(_graph(ns, project), rules)
    except Exception as e:  # noqa: BLE001
        log.warning("rules reproject %s/%s failed: %s", ns, project, e)
        _set_proj(ns, project, "stale", str(e))
        return {"project": project, "state": "stale", "rules": 0, "edges": 0, "error": str(e)}
    with sqldb.connect() as c:
        moved = c.execute("SELECT COUNT(*) FROM kh_rule WHERE ns=? AND project=? AND updated_ms > ?",
                          (ns, project, start)).fetchone()[0]
        cur = c.execute("UPDATE kh_rule_proj SET state='ok', error=NULL, updated_ms=? WHERE ns=? AND project=? "
                        "AND state='rebuilding'", (sqldb.now_ms(c), ns, project)) if not moved else None
        ok = bool(cur is not None and cur.rowcount and cur.rowcount > 0)
        if not ok:
            c.execute("UPDATE kh_rule_proj SET state='stale', error=? WHERE ns=? AND project=?",
                      ("rules changed during the rebuild", ns, project))
    return {"project": project, "state": "ok" if ok else "stale", "rules": len(rules), "edges": n_edges}


def reproject_stale() -> int:
    """Cron `rules_reproject`: rebuild up to 20 stale projections."""
    if not graph_on():
        return 0
    with sqldb.connect() as c:
        if not sqldb.table_exists(c, "kh_rule_proj"):
            return 0
        rows = c.execute(f"SELECT ns, project FROM kh_rule_proj WHERE state='stale' ORDER BY updated_ms "
                         f"LIMIT {REPROJECT_BATCH}").fetchall()
    n = 0
    for ns, project in rows:
        if reproject(ns, project)["state"] == "ok":
            n += 1
    return n


def _ns_projects(ns: str) -> list[str]:
    with sqldb.connect() as c:
        rows = c.execute("SELECT DISTINCT project FROM kh_rule WHERE ns=? UNION "
                         "SELECT DISTINCT project FROM kh_rule_proj WHERE ns=?", (ns, ns)).fetchall()
    return sorted(r[0] for r in rows)


def purge_ns(ns: str) -> None:
    """lifecycle hook (runs before the ns rows are deleted): drop every `<ns>__<project>` graph projection."""
    from app import graph_db

    for project in _ns_projects(ns):
        graph_db.drop_project_graph(f"{ns}__{project}")


def rebuild_ns(ns: str) -> None:
    """lifecycle hook after an import: rebuild the graph projection of every project that has rules."""
    for project in _ns_projects(ns):
        reproject(ns, project)


CRONS = [("rules_reproject", REPROJECT_INTERVAL_S, reproject_stale)]


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _prepared(ctx: Ctx, raw: Any) -> Any:
    return core.apply_body_filters(ctx, "rules", raw)


def put_rule(ctx: Ctx, rid: str, raw: dict, expected: int | None, create_only: bool = False) -> tuple[int, dict]:
    pol = policy(ctx)
    raw = _prepared(ctx, raw)
    if isinstance(raw, dict) and raw.get("rule_id") not in (None, rid):
        raise CapError("invalid", "rule_id in the body differs from the path")
    rule, errs = normalize(ctx, raw)
    if errs:
        raise CapError("validation_failed", "rule is not valid", extra={"errors": errs})
    fenced = bool(pol.get("fenced"))
    core.fence_required(ctx, fenced)
    confirm_ok = can_confirm(ctx)
    with sqldb.connect() as c:
        core.fence(ctx, c, required=fenced)
        cur = _read(c, ctx.ns, rid)
        if cur is not None and not ctx.may_project(cur["project"]):
            raise CapError("not_found", f"rule '{rid}' not found")
        if cur is not None and create_only:
            raise CapError("duplicate", f"rule '{rid}' already exists", extra={"existing_rule_id": rid})
        outcome, _ = _upsert(c, ctx, rid, rule, expected, confirm_ok, sqldb.now_ms(c))
    state = project_after_commit(ctx.ns, rule["project"], [rid])
    with sqldb.connect() as c:
        out = _views(c, ctx.ns, [_read(c, ctx.ns, rid)])[0]
    out["projection"] = state
    return (201 if outcome == "created" else 200), out


def get_rule(ctx: Ctx, rid: str) -> dict:
    with sqldb.connect() as c:
        r = _read(c, ctx.ns, rid)
        if r is None or not ctx.may_project(r["project"]):
            raise CapError("not_found", f"rule '{rid}' not found")
        return _views(c, ctx.ns, [r])[0]


def _visible_sql(ctx: Ctx, project: str | None, sql: list[str], params: list) -> None:
    if project:
        if not ctx.may_project(project):
            raise CapError("forbidden", f"no grant on project '{project}'")
        sql.append("project=?")
        params.append(project)
        return
    eff = ctx.projects()
    if "*" not in eff:
        vis = [""] + list(eff)
        sql.append(f"(project IS NULL OR project IN ({', '.join('?' for _ in vis)}))")
        params += vis


def list_rules(ctx: Ctx, project: str | None, entity: str | None, attribute: str | None, validity: list[str],
               run_id: str | None, limit: int) -> dict:
    sql, params = ["ns=?"], [ctx.ns]
    _visible_sql(ctx, project, sql, params)
    if entity:
        sql.append("entity_key=?")
        params.append(fold(entity))
    if attribute:
        sql.append("attribute_key=?")
        params.append(fold(attribute))
    if validity:
        bad = [v for v in validity if v not in VALIDITY]
        if bad:
            raise CapError("invalid", f"unknown validity: {', '.join(bad)}")
        sql.append(f"validity IN ({', '.join('?' for _ in validity)})")
        params += validity
    if run_id:
        sql.append("rule_id IN (SELECT rule_id FROM kh_rule_run WHERE ns=? AND run_id=?)")
        params += [ctx.ns, run_id]
    with sqldb.connect() as c:
        rows = c.execute(f"SELECT * FROM kh_rule WHERE {' AND '.join(sql)} ORDER BY updated_ms DESC, rule_id "
                         f"LIMIT {int(limit)}", params).fetchall()
        rules = _views(c, ctx.ns, rows)
    return {"rules": rules, "count": len(rules)}


def confirm_rule(ctx: Ctx, rid: str, validity: Any, run_id: Any, expected: int | None) -> dict:
    if not can_confirm(ctx):
        raise CapError("forbidden", "confirming rules needs the confirm right")
    if validity not in VALIDITY or validity == "superseded":
        raise CapError("invalid", "validity must be verified, contradicted, unverified or retired")
    run_id = run_id if run_id is not None else ctx.run_id
    if run_id is not None and (not isinstance(run_id, str) or len(run_id) > REF_MAX):
        raise CapError("invalid", "run_id must be a string")
    with sqldb.connect() as c:
        r = _read(c, ctx.ns, rid)
        if r is None or not ctx.may_project(r["project"]):
            raise CapError("not_found", f"rule '{rid}' not found")
        now = sqldb.now_ms(c)
        core.cas_update(c, "kh_rule", {"ns": ctx.ns, "rule_id": rid},
                        {"validity": validity, "superseded_by": None, "actor_json": ctx.actor_json(),
                         "updated_ms": now}, expected, ns=ctx.ns, what=f"rule '{rid}'")
        _mark_run(c, ctx.ns, run_id, rid, "confirmed", now)
        project = r["project"] or ""
    state = project_after_commit(ctx.ns, project, [rid])
    out = get_rule(ctx, rid)
    out["projection"] = state
    return out


def bulk(ctx: Ctx, body: dict, partial: bool) -> tuple[int, dict]:
    items = body.get("rules")
    if not isinstance(items, list) or not items:
        raise CapError("invalid", "rules must be a non-empty list")
    if len(items) > BULK_MAX:
        raise CapError("too_large", f"at most {BULK_MAX} rules per call", extra={"limit": BULK_MAX})
    top_project = body.get("project")
    top_run = body.get("run_id", ctx.run_id)
    if top_run is not None and (not isinstance(top_run, str) or len(top_run) > REF_MAX):
        raise CapError("invalid", "run_id must be a string")
    pol = policy(ctx)
    confirm_ok = can_confirm(ctx)
    ready: list[tuple[int, dict]] = []
    rejected: list[dict] = []
    for i, raw in enumerate(items):
        try:
            raw = _prepared(ctx, raw)
        except CapError as e:
            rejected.append({"index": i, "errors": [{"pointer": "/", "message": f"{e.code}: {e.detail}"}]})
            continue
        rule, errs = normalize(ctx, raw, top_project, top_run)
        if not errs and rule["validity"] in CONFIRMED and not confirm_ok:
            errs = [{"pointer": "/validity", "message": f"setting validity {rule['validity']} needs the confirm "
                                                         "right"}]
        if errs:
            rejected.append({"index": i, "errors": errs})
            continue
        ready.append((i, rule))
    if rejected and not partial:
        raise CapError("validation_failed", "some rules are not valid", extra={"rejected": rejected})
    fenced = bool(pol.get("fenced"))
    core.fence_required(ctx, fenced)
    accepted, created, duplicates, seen, touched = [], 0, 0, {}, {}
    with sqldb.connect() as c:
        core.fence(ctx, c, required=fenced)
        now = sqldb.now_ms(c)
        for i, rule in ready:
            rid = rule["rule_id"] or ("r_" + rule["rule_hash"][:24])
            if rule["rule_hash"] in seen:
                duplicates += 1
                accepted.append({"index": i, "rule_id": seen[rule["rule_hash"]], "outcome": "duplicate"})
                continue
            try:
                outcome, final_id = _upsert(c, ctx, rid, rule, None, confirm_ok, now, bulk=True)
            except CapError as e:
                if not partial:
                    raise CapError(e.code, f"rules[{i}]: {e.detail}", extra={"index": i, **e.extra})
                rejected.append({"index": i, "errors": [{"pointer": "/", "message": f"{e.code}: {e.detail}"}]})
                continue
            seen[rule["rule_hash"]] = final_id
            if outcome == "created":
                created += 1
            elif outcome == "duplicate":
                duplicates += 1
            touched.setdefault(rule["project"], []).append(final_id)
            accepted.append({"index": i, "rule_id": final_id, "outcome": outcome})
    states = {p: project_after_commit(ctx.ns, p, ids) for p, ids in touched.items()}
    projection = ("off" if not graph_on() else
                  "stale" if "stale" in states.values() else "ok")
    return (201 if created else 200), {"accepted": accepted, "rejected": rejected, "created": created,
                                       "duplicates": duplicates, "projection": projection}


# -- query --------------------------------------------------------------------


def _filter_sql(project: str, validity_in: list[str], min_conf: float | None) -> tuple[str, list]:
    sql = f"ns=? AND project=? AND validity IN ({', '.join('?' for _ in validity_in)})"
    params: list = [project, *validity_in]
    if min_conf is not None:
        sql += " AND confidence >= ?"
        params.append(float(min_conf))
    return sql, params


def _rules_touching_sql(c, ns: str, project: str, keys: list[str], relations: list[str], filt: tuple) -> list:
    out: dict[str, Any] = {}
    fsql, fparams = filt
    for part in _chunks(keys):
        ph = ", ".join("?" for _ in part)
        if "APPLIES_TO" in relations:
            for r in c.execute(f"SELECT * FROM kh_rule WHERE {fsql} AND entity_key IN ({ph})",
                               [ns, *fparams, *part]).fetchall():
                out[r["rule_id"]] = r
        lrels = [x for x in relations if x != "APPLIES_TO"]
        if lrels:
            rph = ", ".join("?" for _ in lrels)
            for r in c.execute(f"SELECT * FROM kh_rule WHERE {fsql} AND rule_id IN (SELECT rule_id FROM "
                               f"kh_rule_link WHERE ns=? AND project=? AND relation IN ({rph}) AND to_key IN ({ph}))",
                               [ns, *fparams, ns, project, *lrels, *part]).fetchall():
                out[r["rule_id"]] = r
    return list(out.values())


def _rules_touching_graph(c, g, ns: str, keys: list[str], relations: list[str], filt: tuple) -> list:
    ids: set[str] = set()
    for part in _chunks(keys):
        nodes, edges = g.fetch_graph([node_ent(k) for k in part], relations, hops=1)
        targets = {node_ent(k) for k in part}
        for e in edges:
            if e["relation"] in relations and e["target_core_id"] in targets and \
                    str(e["source_core_id"]).startswith("rule__"):
                ids.add(e["source_core_id"][len("rule__"):])
    if not ids:
        return []
    fsql, fparams = filt
    out = []
    for part in _chunks(sorted(ids)):
        out += c.execute(f"SELECT * FROM kh_rule WHERE {fsql} AND rule_id IN ({', '.join('?' for _ in part)})",
                         [ns, *fparams, *part]).fetchall()
    return out


def query(ctx: Ctx, q: dict) -> dict:
    unknown = set(q) - {"project", "start_entities", "hops", "relations", "validity_in", "min_confidence", "path"}
    if unknown:
        raise CapError("invalid", f"unknown keys: {', '.join(sorted(unknown))}")
    project = ctx.require_project(q.get("project"))
    starts = q.get("start_entities") or []
    if not isinstance(starts, list) or not starts or len(starts) > START_MAX or \
            not all(isinstance(s, str) for s in starts):
        raise CapError("invalid", f"start_entities must be a list of 1-{START_MAX} entity names")
    hops = core.parse_int(q.get("hops"), "hops", 1, 1, HOPS_MAX)
    relations = q.get("relations") or list(RELATIONS)
    if not isinstance(relations, list) or any(r not in RELATIONS for r in relations):
        raise CapError("invalid", f"relations must be a subset of {', '.join(RELATIONS)}")
    validity_in = q.get("validity_in") or list(LIVE)
    if not isinstance(validity_in, list) or any(v not in VALIDITY for v in validity_in):
        raise CapError("invalid", f"validity_in must be a subset of {', '.join(VALIDITY)}")
    mc = q.get("min_confidence")
    if mc is not None and (isinstance(mc, bool) or not isinstance(mc, (int, float)) or not 0 <= mc <= 1):
        raise CapError("invalid", "min_confidence must be a number in [0, 1]")
    path = q.get("path") or "auto"
    if path not in ("auto", "sql"):
        raise CapError("invalid", "path must be auto or sql")
    start_keys = list(dict.fromkeys(k for k in (fold(s) for s in starts) if k))
    filt = _filter_sql(project, validity_in, mc)
    used, reason, g = "sql", None, None
    with sqldb.connect() as c:
        if path == "auto":
            if not graph_on():
                reason = "graph_off"
            else:
                st = _proj_state(c, ctx.ns, project)
                reason = None if st == "ok" else ("projection_" + (st or "missing"))
    if path == "auto" and reason is None:
        try:
            g = _graph(ctx.ns, project)
            used = "graph"
        except Exception as e:  # noqa: BLE001
            reason = f"graph_error: {e}"[:200]
    reached: dict[str, Any] = {}
    seen_ents = set(start_keys)
    frontier = list(start_keys)
    truncated = False
    with sqldb.connect() as c:
        for _ in range(hops):
            if not frontier:
                break
            if g is not None:
                try:
                    rows = _rules_touching_graph(c, g, ctx.ns, frontier, relations, filt)
                except Exception as e:  # noqa: BLE001
                    g, used, reason = None, "sql", f"graph_error: {e}"[:200]
                    rows = _rules_touching_sql(c, ctx.ns, project, frontier, relations, filt)
            else:
                rows = _rules_touching_sql(c, ctx.ns, project, frontier, relations, filt)
            new = [r for r in rows if r["rule_id"] not in reached]
            for r in sorted(new, key=lambda r: r["rule_id"]):
                if len(reached) >= QUERY_RULES_MAX:
                    truncated = True
                    break
                reached[r["rule_id"]] = r
            if truncated:
                break
            views_new = _views(c, ctx.ns, [r for r in new if r["rule_id"] in reached])
            nxt = []
            for v in views_new:
                keys = ([v["entity_key"]] if "APPLIES_TO" in relations else []) + \
                       [ln["to_key"] for ln in v["links"] if ln["relation"] in relations]
                for k in keys:
                    if k not in seen_ents:
                        seen_ents.add(k)
                        nxt.append(k)
            frontier = nxt
        rules = _views(c, ctx.ns, sorted(reached.values(), key=lambda r: r["rule_id"]))
    edges, entities = [], {}
    for v in rules:
        if "APPLIES_TO" in relations:
            edges.append({"rule_id": v["rule_id"], "relation": "APPLIES_TO", "entity_key": v["entity_key"]})
            entities.setdefault(v["entity_key"], v["entity"])
        for ln in v["links"]:
            if ln["relation"] in relations:
                edges.append({"rule_id": v["rule_id"], "relation": ln["relation"], "entity_key": ln["to_key"]})
                entities.setdefault(ln["to_key"], ln["to_entity"])
    for k in start_keys:
        entities.setdefault(k, next((s for s in starts if fold(s) == k), k))
    conf, conf_trunc = conflicts(rules)
    out = {"project": project, "path": used, "rules": rules, "edges": edges,
           "entities": [{"key": k, "name": n, "start": k in start_keys} for k, n in sorted(entities.items())],
           "related": sorted(k for k in entities if k not in start_keys), "conflicts": conf,
           "truncated": truncated or conf_trunc}
    if reason is not None and used == "sql" and path == "auto":
        out["fallback_reason"] = reason
    return out


# -- scans and gate -------------------------------------------------------------


def _covered(c, ns: str, project: str, keys: list[str]) -> tuple[set[str], set[str]]:
    any_, ver = set(), set()
    live = ", ".join("?" for _ in LIVE)
    for part in _chunks(keys):
        ph = ", ".join("?" for _ in part)
        for k, v in c.execute(f"SELECT entity_key, validity FROM kh_rule WHERE ns=? AND project=? AND validity IN "
                              f"({live}) AND entity_key IN ({ph})", [ns, project, *LIVE, *part]).fetchall():
            any_.add(k)
            if v == "verified":
                ver.add(k)
    return any_, ver


def _scan_view(r, full: bool) -> dict:
    out = {"scan_id": r["scan_id"], "project": r["project"] or "", "run_id": r["run_id"],
           "n_scanned": int(r["n_scanned"]), "n_any": int(r["n_any"]), "n_verified": int(r["n_verified"]),
           "coverage_any": r["coverage_any"], "coverage_verified": r["coverage_verified"],
           "actor": core.loads(r["actor_json"], None), "created_ms": r["created_ms"]}
    if full:
        res = core.loads(r["result_json"], {}) or {}
        out["uncovered"] = res.get("uncovered", [])
        out["unverified_only"] = res.get("unverified_only", [])
    return out


def scan(ctx: Ctx, body: dict) -> tuple[int, dict]:
    project = ctx.require_project(body.get("project"))
    sid = body.get("scan_id") or core.new_id()
    if not isinstance(sid, str) or not ID_RE.match(sid):
        raise CapError("invalid", "scan_id must be 1-128 characters of [A-Za-z0-9_.-]")
    ents = body.get("entities_scanned")
    if not isinstance(ents, list) or len(ents) > SCAN_ENTITIES_MAX or not all(isinstance(e, str) for e in ents):
        raise CapError("invalid", f"entities_scanned must be a list of at most {SCAN_ENTITIES_MAX} names")
    run_id = body.get("run_id", ctx.run_id)
    if run_id is not None and (not isinstance(run_id, str) or len(run_id) > REF_MAX):
        raise CapError("invalid", "run_id must be a string")
    names: dict[str, str] = {}
    for e in ents:
        k = fold(e)
        if k:
            names.setdefault(k, e)
    keys = sorted(names)
    sha = core.sha256_json([project, keys])
    with sqldb.connect() as c:
        old = c.execute("SELECT * FROM kh_rule_scan WHERE ns=? AND scan_id=?", (ctx.ns, sid)).fetchone()
        if old is not None:
            if old["request_sha"] != sha:
                raise CapError("duplicate", f"scan '{sid}' exists with another entity set",
                               extra={"scan_id": sid})
            return 200, _scan_view(old, True)
        any_, ver = _covered(c, ctx.ns, project, keys)
        n = len(keys)
        res = {"uncovered": [names[k] for k in keys if k not in any_][:SCAN_LIST_CAP],
               "unverified_only": [names[k] for k in keys if k in any_ and k not in ver][:SCAN_LIST_CAP]}
        row = {"ns": ctx.ns, "scan_id": sid, "project": project, "run_id": run_id, "request_sha": sha,
               "n_scanned": n, "n_any": len(any_), "n_verified": len(ver),
               "coverage_any": round(len(any_) / n, 6) if n else 0.0,
               "coverage_verified": round(len(ver) / n, 6) if n else 0.0, "result_json": core.dumps(res),
               "actor_json": ctx.actor_json(), "created_ms": sqldb.now_ms(c)}
        if not sqldb.insert_new(c, "kh_rule_scan", row):
            raise CapError("duplicate", f"scan '{sid}' was written concurrently", extra={"scan_id": sid})
        r = c.execute("SELECT * FROM kh_rule_scan WHERE ns=? AND scan_id=?", (ctx.ns, sid)).fetchone()
        return 201, _scan_view(r, True)


def list_scans(ctx: Ctx, project: str | None, limit: int) -> dict:
    sql, params = ["ns=?"], [ctx.ns]
    _visible_sql(ctx, project, sql, params)
    with sqldb.connect() as c:
        rows = c.execute(f"SELECT * FROM kh_rule_scan WHERE {' AND '.join(sql)} ORDER BY created_ms DESC, scan_id "
                         f"LIMIT {int(limit)}", params).fetchall()
    scans = [_scan_view(r, False) for r in rows]
    return {"scans": scans, "count": len(scans)}


def get_scan(ctx: Ctx, sid: str) -> dict:
    with sqldb.connect() as c:
        r = c.execute("SELECT * FROM kh_rule_scan WHERE ns=? AND scan_id=?", (ctx.ns, sid)).fetchone()
    if r is None or not ctx.may_project(r["project"]):
        raise CapError("not_found", f"scan '{sid}' not found")
    return _scan_view(r, True)


def gate(ctx: Ctx, body: dict) -> dict:
    run_id = body.get("run_id") or ctx.run_id
    if not isinstance(run_id, str) or not run_id or len(run_id) > REF_MAX:
        raise CapError("invalid", "run_id is required")
    project = ctx.require_project(body.get("project"))
    ents = body.get("entities") or []
    if not isinstance(ents, list) or len(ents) > SCAN_ENTITIES_MAX or not all(isinstance(e, str) for e in ents):
        raise CapError("invalid", f"entities must be a list of at most {SCAN_ENTITIES_MAX} names")
    names: dict[str, str] = {}
    for e in ents:
        if fold(e):
            names.setdefault(fold(e), e)
    live = ", ".join("?" for _ in LIVE)
    with sqldb.connect() as c:
        run_rows = c.execute(f"SELECT * FROM kh_rule WHERE ns=? AND project=? AND validity IN ({live}) AND rule_id IN "
                             f"(SELECT rule_id FROM kh_rule_run WHERE ns=? AND run_id=?) ORDER BY rule_id",
                             [ctx.ns, project, *LIVE, ctx.ns, run_id]).fetchall()
        run_rules = _views(c, ctx.ns, run_rows)
        in_run = {r["entity_key"] for r in run_rules}
        dep_keys = sorted({ln["to_key"] for r in run_rules for ln in r["links"] if ln["relation"] == "DEPENDS_ON"})
        dep_live, _ = _covered(c, ctx.ns, project, dep_keys)
    missing = [names[k] for k in sorted(names) if k not in in_run]
    orphans = []
    for r in run_rules:
        if not r["quote_id"] and not r["source_text"]:
            orphans.append({"rule_id": r["rule_id"], "reason": "no_source"})
        for ln in r["links"]:
            if ln["relation"] == "DEPENDS_ON" and ln["to_key"] not in dep_live:
                orphans.append({"rule_id": r["rule_id"], "reason": "dangling_dependency",
                                "to_entity": ln["to_entity"]})
    return {"pass": not missing and not orphans, "run_id": run_id, "project": project, "n_entities": len(names),
            "n_rules": len(run_rules), "missing": missing[:SCAN_LIST_CAP], "orphans": orphans[:SCAN_LIST_CAP]}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _check_id(rid: str) -> str:
    if not ID_RE.match(rid or ""):
        raise CapError("invalid", "rule id must be 1-128 characters of [A-Za-z0-9_.-]")
    return rid


def _flag(v: str | None) -> bool:
    return v is not None and v.strip().lower() not in ("false", "0", "no", "off")


def attach(app: FastAPI) -> None:
    tag = ["v1 rules"]

    @app.post("/v1/{ns}/rules:bulk", tags=tag, summary="Write many rules (dedupe by content hash)")
    @core.idempotent
    async def post_bulk(request: Request, ns: str, partial: str | None = Query(None),
                        ctx: Ctx = Depends(core.ns_ctx)):
        _require(ctx, "write")
        body = await core.read_object(request)
        status, out = await core.run(bulk, ctx, body, _flag(partial))
        return core.reply(out, status=status)

    @app.post("/v1/{ns}/rules:query", tags=tag, summary="Walk the rule graph from start entities")
    @core.idempotent
    async def post_query(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require(ctx, "read")
        return core.reply(await core.run(query, ctx, await core.read_object(request)))

    @app.post("/v1/{ns}/rules:gate", tags=tag, summary="Run gate: every entity covered, no orphans")
    @core.idempotent
    async def post_gate(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require(ctx, "read")
        return core.reply(await core.run(gate, ctx, await core.read_object(request)))

    @app.post("/v1/{ns}/rules:reproject", tags=tag, summary="Rebuild the graph projection of a project")
    @core.idempotent
    async def post_reproject(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        if not (ctx.is_admin or ctx.role == "orchestrator"):
            raise CapError("forbidden", "only the namespace admin or an orchestrator may reproject")
        body = await core.read_object(request)
        project = ctx.require_project(body.get("project"))
        return core.reply(await core.run(reproject, ctx.ns, project))

    @app.post("/v1/{ns}/rules/scans", tags=tag, summary="Record a coverage scan")
    @core.idempotent
    async def post_scan(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require(ctx, "write")
        status, out = await core.run(scan, ctx, await core.read_object(request))
        return core.reply(out, status=status)

    @app.get("/v1/{ns}/rules/scans", tags=tag, summary="List coverage scans")
    async def get_scans(ns: str, project: str | None = Query(None), limit: str | None = Query(None),
                        ctx: Ctx = Depends(core.ns_ctx)):
        _require(ctx, "read")
        return await core.run(list_scans, ctx, project, core.clamp_limit(limit, 100, SCANS_LIST_MAX))

    @app.get("/v1/{ns}/rules/scans/{scan_id}", tags=tag, summary="Read one coverage scan")
    async def get_one_scan(ns: str, scan_id: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require(ctx, "read")
        return await core.run(get_scan, ctx, scan_id)

    @app.post("/v1/{ns}/rules/{rule_id}:confirm", tags=tag, summary="Set the validity of a rule (confirm right)")
    @core.idempotent
    async def post_confirm(request: Request, ns: str, rule_id: str, ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require("rules", "read")
        body = await core.read_object(request)
        out = await core.run(confirm_rule, ctx, _check_id(rule_id), body.get("validity"), body.get("run_id"),
                             core.if_match(request))
        return core.reply(out, etag_rev=out["rev"])

    @app.get("/v1/{ns}/rules", tags=tag, summary="List rules")
    async def get_rules(ns: str, project: str | None = Query(None), entity: str | None = Query(None),
                        attribute: str | None = Query(None), validity: str | None = Query(None),
                        run_id: str | None = Query(None), limit: str | None = Query(None),
                        ctx: Ctx = Depends(core.ns_ctx)):
        _require(ctx, "read")
        return await core.run(list_rules, ctx, project, entity, attribute, core.csv(validity), run_id,
                              core.clamp_limit(limit, 100, LIST_MAX))

    @app.get("/v1/{ns}/rules/{rule_id}", tags=tag, summary="Read one rule")
    async def get_one(ns: str, rule_id: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require(ctx, "read")
        out = await core.run(get_rule, ctx, _check_id(rule_id))
        return core.reply(out, etag_rev=out["rev"])

    @app.put("/v1/{ns}/rules/{rule_id}", tags=tag, summary="Create or replace a rule")
    async def put_one(request: Request, ns: str, rule_id: str, ctx: Ctx = Depends(core.ns_ctx)):
        pol = _require(ctx, "write")
        _check_id(rule_id)
        create_only = (request.headers.get("if-none-match") or "").strip() == "*"
        expected = core.require_if_match(request, bool(pol.get("require_if_match")) and not create_only)
        body = await core.read_object(request)
        status, out = await core.run(put_rule, ctx, rule_id, body, expected, create_only)
        return core.reply(out, status=status, etag_rev=out["rev"])
