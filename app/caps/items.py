"""items (spec §6.9): distilled knowledge items (lessons) of a namespace.

SQL (`kh_item`) is the system of record; the vector row in collection `ns_<ns>_knowledge` (KH's vector store, same
embedder as core, `encode` never `encode_query`) is a search projection written after commit and tracked by
`vec_rev`. Core (people's) lessons stay in the core vector store; the two never mix.

Public helpers for other modules: `transition(ctx, item_id, to, reason=..., superseded_by=None,
expected_rev=None, enforce=True)`, `rollback_transition(c, ns, item_id, reason)` (metrics rollback, in-transaction),
`search(ns, text, k)`, `lint(st, text, guard_names)`, `key_of(st, keyspace, key)`.
"""
from __future__ import annotations

import os
import re
from typing import Any

from fastapi import Depends, FastAPI, Query, Request

from app import config, sqldb
from app.caps import core
from app.caps.core import CapError, Ctx

NAME = "items"
VERSION = "1.0.0"
STATUS = "beta"
DEFAULT_ON = True
TEXT_MAX = 4000
LINT_MAX = 400
TTL_DEFAULT, TTL_MIN, TTL_MAX = 90, 1, 3650
PEOPLE_TTL_MAX = 90
CROSS_AFTER = 3
LIST_MAX = 500
PROVENANCE_MAX = 500
GUARDS_MAX = 20
TRIGGERS_MAX = 8
NEAR_DUP_POOL = 50
TRIGGER_RE = re.compile(r"^[a-z0-9][a-z0-9_.:\-]{0,63}$")
LIMITS = {"text_chars": TEXT_MAX, "lint_chars": LINT_MAX, "ttl_days": [TTL_MIN, TTL_MAX], "ttl_default_days": TTL_DEFAULT,
          "cross_after_projects": CROSS_AFTER, "list": LIST_MAX, "provenance_ids": PROVENANCE_MAX,
          "guards": GUARDS_MAX, "triggers": TRIGGERS_MAX, "near_dup_pool": NEAR_DUP_POOL}
TABLES = ["kh_item", "kh_item_support", "kh_item_confirm"]
LOCAL_TABLES: list[str] = []
DAY_MS = 86_400_000

STATUSES = ("proposed", "active", "contested", "superseded", "withdrawn")
LIVE = ("proposed", "active", "contested")
TRANSITIONS = {
    "proposed": {"active", "contested", "superseded", "withdrawn"},
    "active": {"active", "contested", "superseded", "withdrawn"},
    "contested": {"active", "proposed", "superseded", "withdrawn"},
    "superseded": {"proposed"},
    "withdrawn": {"proposed"},
}
BOUNDS = ("project", "generic", "people")
ITEM_VERBS = ("support",)

HEDGES = ("maybe", "might", "probably", "perhaps", "possibly", "i think", "it seems", "seems to", "sort of",
          "kind of", "i guess")
VERBS = frozenset("""
accept access account achieve acknowledge acquire act activate adapt add address adjust admit adopt advance advise
affirm aggregate alert align allocate allow alter analyse analyze anchor annotate announce answer anticipate append
apply appoint approve archive argue arrange ask assemble assert assess assign assist associate assume attach attempt
attend audit authenticate authorize automate avoid back backfill backup balance ban base batch begin benchmark bind
block book boost bound break bring broadcast budget build bump bundle calculate calibrate call cancel cap capture
care carry cascade cast catch categorize cause centralize certify chain challenge change charge chart check choose
cite claim clarify classify clean cleanse clear click clone close cluster coach code collaborate collapse collect
combine comment commit communicate compare compile complete comply compose compress compute concatenate conclude
configure confirm connect consider consolidate consult contact contain continue contribute control convert copy
correct correlate count cover create credit cross cut deactivate debug decide declare decline decode decommission
decouple decrease decrypt dedupe deduplicate default defer define delegate delete deliver demand demonstrate deny
deploy deprecate derive describe design detach detect determine develop diagnose diff disable discard disclose
disconnect discover discuss dismiss dispatch display distinguish distribute divide document double download draft
drain draw drop dump duplicate echo edit educate elevate eliminate embed emit employ empty emulate enable encode
encourage encrypt end enforce engage enhance enlist enqueue ensure enter enumerate escalate escape establish
estimate evaluate examine exclude execute exercise expand expect expire explain explore export expose extend
extract facilitate fail fallback favor fetch file fill filter finalize find fix flag flush focus fold follow force
forecast fork format forward free freeze fulfill gather generate get give grant group guard guarantee guide halt
handle hash hide highlight hold identify ignore implement import improve include increase increment index inform
initialize inject input insert inspect install instantiate instruct integrate interpret introduce invalidate
investigate invite invoke isolate issue iterate join justify keep kill label launch lead learn leave leverage limit
link list load localize locate lock log look lower maintain make manage map mark mask match measure merge migrate
minimize mirror mitigate model modify monitor mount move multiply name navigate negotiate normalize note notify
observe obtain offer omit open operate optimize order organize output override own pack page paginate parse partition
pass patch pause perform persist pick pin ping place plan play poll populate post postpone precompute prefer prefix
prepare present preserve prevent preview print prioritize probe process produce profile program promote prompt
propagate propose protect prove provide provision prune publish pull purge push put qualify query question queue
quote raise rank rate reach read rebase rebuild recalculate receive recheck reclaim recommend reconcile record
recover redact redeploy redirect reduce refactor refer refine refresh refuse regenerate register reindex reject
relate release reload rely remind remove rename render renew reopen reorder repair repeat rephrase replace replay
replicate reply report request require rerun reserve reset resize resolve respect respond restart restore restrict
resume retain retire retry return reuse review revise revoke rewrite roll rollback rotate round route run sample
sanitize save scale scan schedule scope score screen script search secure seed select send separate sequence serve
set settle share shift ship show shrink shut sign simplify simulate size skip slice snapshot sort specify split
squash stage standardize start state stop store stream strip structure submit subscribe substitute subtract suggest
summarize supersede supply support suppress surface suspend swap switch sync synchronize tag take target teach tell
terminate test throttle tie time toggle track trace train transfer transform translate treat trigger trim truncate
trust try tune turn unblock uncomment undo uninstall unlock unpack unpin unregister unset unsubscribe untag update
upgrade upload upsert use validate verify version view visit wait warm warn watch weigh whitelist widen wire withdraw
wrap write yield zip
""".split())


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _m1(c) -> None:
    sqldb.create_table(
        c, "kh_item",
        "CREATE TABLE IF NOT EXISTS kh_item (ns TEXT NOT NULL, item_id TEXT NOT NULL, kind TEXT NOT NULL, "
        "keyspace TEXT NOT NULL, key_json TEXT, key_sha TEXT NOT NULL, project TEXT NOT NULL DEFAULT '', "
        "scope TEXT NOT NULL, status TEXT NOT NULL, status_reason TEXT, title TEXT, text TEXT, text_sha TEXT, "
        "attrs_json TEXT, provenance_json TEXT, evidence_count INTEGER NOT NULL DEFAULT 0, support_projects_json TEXT, "
        "guard_names_json TEXT, guard_metric TEXT, metric_dir TEXT, ttl_days INTEGER, expires_ms INTEGER, "
        "activated_ms INTEGER, last_confirmed_ms INTEGER, superseded_by TEXT, actor_json TEXT, "
        "rev INTEGER NOT NULL DEFAULT 1, vec_rev INTEGER NOT NULL DEFAULT 0, created_ms INTEGER, updated_ms INTEGER, "
        "PRIMARY KEY (ns, item_id))",
        "CREATE COLUMN TABLE kh_item (ns NVARCHAR(64) NOT NULL, item_id NVARCHAR(64) NOT NULL, "
        "kind NVARCHAR(128) NOT NULL, keyspace NVARCHAR(64) NOT NULL, key_json NCLOB, key_sha NVARCHAR(64) NOT NULL, "
        "project NVARCHAR(128) NOT NULL, scope NVARCHAR(16) NOT NULL, status NVARCHAR(16) NOT NULL, "
        "status_reason NVARCHAR(512), title NVARCHAR(512), text NCLOB, text_sha NVARCHAR(64), attrs_json NCLOB, "
        "provenance_json NCLOB, evidence_count BIGINT NOT NULL, support_projects_json NCLOB, guard_names_json NCLOB, "
        "guard_metric NVARCHAR(256), metric_dir NVARCHAR(8), ttl_days BIGINT, expires_ms BIGINT, activated_ms BIGINT, "
        "last_confirmed_ms BIGINT, superseded_by NVARCHAR(64), actor_json NCLOB, rev BIGINT NOT NULL, "
        "vec_rev BIGINT NOT NULL, created_ms BIGINT, updated_ms BIGINT, PRIMARY KEY (ns, item_id))")
    sqldb.create_index(c, "kh_item_key", "kh_item", ["ns", "key_sha", "kind"])
    sqldb.create_index(c, "kh_item_status", "kh_item", ["ns", "status", "expires_ms"])
    sqldb.create_table(
        c, "kh_item_support",
        "CREATE TABLE IF NOT EXISTS kh_item_support (ns TEXT NOT NULL, item_id TEXT NOT NULL, project TEXT NOT NULL, "
        "run_id TEXT NOT NULL, obo TEXT, principal TEXT, at_ms INTEGER, PRIMARY KEY (ns, item_id, project, run_id))",
        "CREATE COLUMN TABLE kh_item_support (ns NVARCHAR(64) NOT NULL, item_id NVARCHAR(64) NOT NULL, "
        "project NVARCHAR(128) NOT NULL, run_id NVARCHAR(256) NOT NULL, obo NVARCHAR(256), principal NVARCHAR(256), "
        "at_ms BIGINT, PRIMARY KEY (ns, item_id, project, run_id))")
    sqldb.create_table(
        c, "kh_item_confirm",
        "CREATE TABLE IF NOT EXISTS kh_item_confirm (ns TEXT NOT NULL, item_id TEXT NOT NULL, principal TEXT NOT NULL, "
        "at_ms INTEGER, PRIMARY KEY (ns, item_id, principal))",
        "CREATE COLUMN TABLE kh_item_confirm (ns NVARCHAR(64) NOT NULL, item_id NVARCHAR(64) NOT NULL, "
        "principal NVARCHAR(256) NOT NULL, at_ms BIGINT, PRIMARY KEY (ns, item_id, principal))")


def _m2(c) -> None:
    sqldb.add_column(c, "kh_item", "bound", "TEXT", "NVARCHAR(16)")
    sqldb.add_column(c, "kh_item", "triggers_json", "TEXT", "NCLOB")


def _usage(c, ns: str, type_: str) -> int:
    if not sqldb.table_exists(c, "kh_item"):
        return 0
    return int(c.execute("SELECT COUNT(*) FROM kh_item WHERE ns=? AND kind=?", (ns, type_)).fetchone()[0])


def init(c) -> None:
    sqldb.migrate(c, NAME, [(1, _m1), (2, _m2)])
    if _usage not in core.TYPE_USAGE:
        core.TYPE_USAGE.append(_usage)


# ---------------------------------------------------------------------------
# Settings / helpers
# ---------------------------------------------------------------------------


def title_chars() -> int:
    return config._int("KH_DISTILL_TITLE_CHARS", 120)


def same_floor() -> float:
    return config._float("KH_DISTILL_SAME", 0.95)


def collection_name(ns: str) -> str:
    return f"ns_{ns}_knowledge"


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def text_sha(text: str) -> str:
    return core.sha256_text(_norm(text))


def key_of(st: core.NsState, keyspace: Any, key: Any) -> tuple[dict, list[str], str, str]:
    """-> (key dict, values, key_sha, wild_sha). Missing dims = '*'."""
    if not isinstance(keyspace, str) or keyspace not in st.keyspaces:
        raise CapError("invalid", f"keyspace '{keyspace}' is not declared in the manifest",
                       extra={"keyspaces": sorted(st.keyspaces)})
    dims = list(st.keyspaces[keyspace]["dims"])
    if key is None or key == "":
        kd: dict = {}
    elif isinstance(key, dict):
        kd = key
    elif isinstance(key, str):
        s = key.strip()
        if s.startswith("{"):
            kd = core.loads(s, None)
            if not isinstance(kd, dict):
                raise CapError("invalid", "key is not valid JSON")
        else:
            kd = {}
            for part in s.split(";"):
                if not part.strip():
                    continue
                d, sep, v = part.partition("=")
                if not sep:
                    raise CapError("invalid", "key must be {dim: value}, 'dim=v;dim=v' or JSON")
                kd[d.strip()] = v.strip()
    else:
        raise CapError("invalid", "key must be {dim: value}, 'dim=v;dim=v' or JSON")
    unknown = [d for d in kd if d not in dims]
    if unknown:
        raise CapError("invalid", f"unknown key dims {unknown}; keyspace '{keyspace}' has {dims}")
    values = []
    for d in dims:
        v = kd.get(d)
        values.append("*" if v is None or str(v).strip() == "" else str(v).strip())
    ksha = core.sha256_text(core.canonical([keyspace, values]))[:40]
    wild = core.sha256_text(core.canonical([keyspace, values[:-1] + ["*"]]))[:40]
    return dict(zip(dims, values)), values, ksha, wild


def policy_of(st: core.NsState, trow: dict | None) -> dict:
    p = (trow or {}).get("confirm_policy") or {}
    if not p:
        p = st.settings("items").get("confirm_policy") or {}
    return p if isinstance(p, dict) else {}


def _policy_allows(ctx: Ctx, policy: dict) -> bool:
    if ctx.is_admin:
        return True
    if ctx.role in (policy.get("roles") or []):
        return True
    email = (ctx.caller.email or "").lower()
    return ctx.caller.kind == "person" and bool(email) and email in {str(e).lower() for e in policy.get("emails") or []}


def _has_policy(policy: dict) -> bool:
    return bool(policy.get("roles") or policy.get("emails"))


def may_transition(ctx: Ctx, policy: dict, to: str) -> bool:
    if ctx.is_admin:
        return True
    pol = _policy_allows(ctx, policy)
    if to in ("active", "proposed"):
        return pol if _has_policy(policy) else ctx.allows("items", "confirm")
    if to == "withdrawn":
        return pol or ctx.allows("items", "withdraw")
    if to == "contested":
        return pol or any(ctx.allows("items", a) for a in ("write", "confirm", "withdraw"))
    if to == "superseded":
        return pol or ctx.allows("items", "write")
    return False


def cross_after(st: core.NsState, keyspace: str) -> int:
    ks = (st.keyspaces.get(keyspace) or {}).get("config") or {}
    v = ks.get("cross_after_projects", st.settings("items").get("cross_after_projects", CROSS_AFTER))
    try:
        return max(1, int(v))
    except (TypeError, ValueError):
        return CROSS_AFTER


def ttl_of(st: core.NsState, row: dict) -> int:
    ttl = row.get("ttl_days") or st.settings("items").get("ttl_days") or TTL_DEFAULT
    try:
        ttl = int(ttl)
    except (TypeError, ValueError):
        ttl = TTL_DEFAULT
    if (row.get("bound") or "project") == "people":
        ttl = min(ttl, PEOPLE_TTL_MAX)
    return max(TTL_MIN, min(TTL_MAX, ttl))


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------


def _guard_exists(c, st: core.NsState, name: str) -> bool:
    s = st.settings("items")
    gtype = s.get("guard_type")
    if not gtype or not sqldb.table_exists(c, "kh_record"):
        return False
    trow = st.type(gtype) or {}
    field = s.get("guard_field")
    col = ((trow.get("index_fields") or {}).get(field)) if field else None
    where = f"{col}=?" if col else "id=?"
    r = c.execute(f"SELECT 1 FROM kh_record WHERE ns=? AND type=? AND {where} AND "
                  "(state IS NULL OR state<>'retired')", (st.ns, gtype, name)).fetchone()
    return r is not None


def lint(st: core.NsState, text: str, guard_names: list | None = None, c=None) -> list[str]:
    conf = st.settings("items").get("lint") or {}
    max_chars = int(conf.get("max_chars") or LINT_MAX)
    verbs = VERBS | {str(v).lower() for v in conf.get("verbs") or []}
    hedges = [str(h).lower() for h in (conf.get("hedges") if conf.get("hedges") is not None else HEDGES)]
    errs = []
    t = (text or "").strip()
    if len(t) > max_chars:
        errs.append(f"text is longer than {max_chars} characters")
    words = re.findall(r"[A-Za-z][A-Za-z'-]*", t)
    first = words[0].lower() if words else ""
    if first not in verbs and first not in ("never", "always"):
        errs.append("start with an imperative verb (or never/always)")
    low = " " + " ".join(re.findall(r"[a-z']+", t.lower())) + " "
    for h in hedges:
        if f" {h} " in low:
            errs.append(f"no hedges: '{h}'")
    names = [g for g in (guard_names or []) if g]
    if names:
        def check(conn):
            for g in names:
                if not _guard_exists(conn, st, g):
                    errs.append(f"guard '{g}' is not a live {st.settings('items').get('guard_type') or 'guard'} record")
        if c is not None:
            check(c)
        else:
            with sqldb.connect() as conn:
                check(conn)
    return errs


# ---------------------------------------------------------------------------
# Rows / views
# ---------------------------------------------------------------------------


def _read(c, ns: str, item_id: str):
    return c.execute("SELECT * FROM kh_item WHERE ns=? AND item_id=?", (ns, item_id)).fetchone()


def _dict(r) -> dict:
    return {k: r[k] for k in r.keys()}


def view(r) -> dict:
    d = _dict(r) if not isinstance(r, dict) else r
    return {
        "item_id": d["item_id"], "kind": d["kind"], "keyspace": d["keyspace"], "key": core.loads(d["key_json"], {}),
        "key_sha": d["key_sha"], "project": d["project"], "scope": d["scope"], "status": d["status"],
        "status_reason": d["status_reason"], "title": d["title"] or "", "text": d["text"] or "",
        "attrs": core.loads(d["attrs_json"], {}), "provenance": core.loads(d["provenance_json"], {}),
        "evidence_count": int(d["evidence_count"] or 0),
        "support_projects": core.loads(d["support_projects_json"], []),
        "guard_names": core.loads(d["guard_names_json"], []), "guard_metric": d["guard_metric"],
        "metric_dir": d["metric_dir"], "ttl_days": d["ttl_days"], "expires_ms": d["expires_ms"],
        "activated_ms": d["activated_ms"], "last_confirmed_ms": d["last_confirmed_ms"],
        "superseded_by": d["superseded_by"], "bound": d.get("bound") or "project",
        "triggers": core.loads(d.get("triggers_json"), []), "actor": core.loads(d["actor_json"], {}),
        "rev": int(d["rev"]), "created_ms": d["created_ms"], "updated_ms": d["updated_ms"],
    }


def visible(ctx: Ctx, row: dict) -> bool:
    return row.get("scope") == "cross" or ctx.may_project(row.get("project"))


def _get_visible(c, ctx: Ctx, item_id: str):
    r = _read(c, ctx.ns, item_id)
    if r is None or not visible(ctx, _dict(r)):
        raise CapError("not_found", f"item '{item_id}' not found")
    return r


# ---------------------------------------------------------------------------
# Vector projection
# ---------------------------------------------------------------------------


def _embedder():
    from app.embeddings import get_embedder

    return get_embedder()


def embed_text(row: dict) -> str:
    title = (row.get("title") or "").strip()
    return f"{title}. {row.get('text') or ''}" if title else (row.get("text") or "")


def _project_rows(ns: str, rows: list[dict]) -> int:
    """Upsert vector rows and mark vec_rev (after commit)."""
    if not rows:
        return 0
    from app import vector_store as vs

    try:
        emb = _embedder()
        vecs = emb.encode([embed_text(r) for r in rows], normalize_embeddings=True)
    except Exception:  # noqa: BLE001
        return 0
    coll = vs.get_or_create(collection_name(ns))
    metas = [{"ns": ns, "project": r["project"], "scope": r["scope"], "status": r["status"], "kind": r["kind"],
              "keyspace": r["keyspace"], "key_sha": r["key_sha"]} for r in rows]
    vs.upsert_rows(coll, [r["item_id"] for r in rows], list(vecs), [embed_text(r) for r in rows], metas)
    with sqldb.connect() as c:
        for r in rows:
            c.execute("UPDATE kh_item SET vec_rev=? WHERE ns=? AND item_id=? AND rev=?",
                      (int(r["rev"]), ns, r["item_id"], int(r["rev"])))
    return len(rows)


def project_ids(ns: str, ids: list[str]) -> int:
    if not ids:
        return 0
    with sqldb.connect() as c:
        rows = [_dict(r) for r in (_read(c, ns, i) for i in ids) if r is not None]
    return _project_rows(ns, rows)


def search(ns: str, text: str, k: int) -> list[dict]:
    """[{item_id, score}] among active rows of the projection (callers re-check SQL)."""
    from app import vector_store as vs

    coll = vs.get_existing(collection_name(ns))
    if coll is None or not text:
        return []
    vec = _embedder().encode(text, normalize_embeddings=True)
    hits = vs.query_vt(coll, vec, max(1, int(k)), where={"status": "active"})
    return [{"item_id": h["vector_id"], "score": float(h["score"])} for h in hits]


def reindex(ns: str | None = None, project: str | None = None, all_rows: bool = False, limit: int = 2000) -> int:
    with sqldb.connect() as c:
        if not sqldb.table_exists(c, "kh_item"):
            return 0
        q = "SELECT * FROM kh_item WHERE 1=1"
        p: list = []
        if ns:
            q += " AND ns=?"
            p.append(ns)
        if project:
            q += " AND project=?"
            p.append(project)
        if not all_rows:
            q += " AND vec_rev<>rev"
        q += f" ORDER BY updated_ms LIMIT {int(limit)}"
        rows = [_dict(r) for r in c.execute(q, p).fetchall()]
    n = 0
    by_ns: dict[str, list] = {}
    for r in rows:
        by_ns.setdefault(r["ns"], []).append(r)
    for k, rs in by_ns.items():
        n += _project_rows(k, rs)
    return n


# ---------------------------------------------------------------------------
# Support / promotion / activation
# ---------------------------------------------------------------------------


def _independent_projects(c, ns: str, item_id: str) -> int:
    runs: dict[str, set] = {}
    for r in c.execute("SELECT project, run_id FROM kh_item_support WHERE ns=? AND item_id=?", (ns, item_id)).fetchall():
        runs.setdefault(r[0], set()).add(r[1] or "")
    n = 0
    for p, rs in runs.items():
        others = set().union(*[v for q, v in runs.items() if q != p]) if len(runs) > 1 else set()
        if any(run == "" or run not in others for run in rs):
            n += 1
    return n


def _merge_prov(old: dict, runs: list, event_ids: list) -> dict:
    o = dict(old or {})
    r = list(o.get("runs") or [])
    e = list(o.get("event_ids") or [])
    for x in runs or []:
        if x and x not in r:
            r.append(x)
    for x in event_ids or []:
        if x and x not in e:
            e.append(x)
    o["runs"] = r[-PROVENANCE_MAX:]
    o["event_ids"] = e[-PROVENANCE_MAX:]
    return o


def _activate_sets(st: core.NsState, row: dict, now: int, reason: str) -> dict:
    sets = {"status": "active", "status_reason": reason, "last_confirmed_ms": now,
            "expires_ms": now + ttl_of(st, row) * DAY_MS, "updated_ms": now}
    if row["status"] != "active" or not row.get("activated_ms"):
        sets["activated_ms"] = now
    return sets


def _add_support(c, ctx: Ctx, row: dict, project: str, runs: list[str], event_ids: list) -> dict:
    """One support row per (project, run). Returns {new_support, promoted, auto_active, row}."""
    ns, iid = ctx.ns, row["item_id"]
    now = sqldb.now_ms(c)
    new = False
    for run in (runs or [""]):
        if sqldb.insert_new(c, "kh_item_support", {"ns": ns, "item_id": iid, "project": project or "",
                                                   "run_id": (run or "")[:256], "obo": ctx.caller.on_behalf_of,
                                                   "principal": ctx.principal, "at_ms": now}):
            new = True
    n = int(c.execute("SELECT COUNT(*) FROM kh_item_support WHERE ns=? AND item_id=?", (ns, iid)).fetchone()[0])
    projs = sorted({r[0] for r in c.execute("SELECT project FROM kh_item_support WHERE ns=? AND item_id=?",
                                            (ns, iid)).fetchall()})
    prov = _merge_prov(core.loads(row.get("provenance_json"), {}), [r for r in runs if r], event_ids)
    sets: dict = {"evidence_count": n, "support_projects_json": core.dumps(projs),
                  "provenance_json": core.dumps(prov), "updated_ms": now}
    promoted = False
    if (row.get("bound") or "project") == "generic" and row["scope"] != "cross":
        if _independent_projects(c, ns, iid) >= cross_after(ctx.st, row["keyspace"]):
            sets["scope"] = "cross"
            promoted = True
    auto = False
    pol = policy_of(ctx.st, ctx.st.type(row["kind"]))
    k = pol.get("auto_after_evidence")
    if row["status"] == "proposed" and isinstance(k, int) and not isinstance(k, bool) and k > 0 and n >= k:
        if not lint(ctx.st, row.get("text") or "", core.loads(row.get("guard_names_json"), []), c):
            sets.update(_activate_sets(ctx.st, row, now, "auto_after_evidence"))
            auto = True
    c.execute(f"UPDATE kh_item SET {', '.join(f'{k}=?' for k in sets)}, rev=rev+1 WHERE ns=? AND item_id=?",
              [*sets.values(), ns, iid])
    return {"new_support": new, "promoted": promoted, "auto_active": auto, "row": _dict(_read(c, ns, iid))}


# ---------------------------------------------------------------------------
# Propose
# ---------------------------------------------------------------------------


def _str_list(v: Any, name: str, maximum: int, rx: re.Pattern | None = None) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise CapError("invalid", f"{name} must be a list of non-empty strings")
    if len(v) > maximum:
        raise CapError("invalid", f"at most {maximum} {name}")
    if rx is not None:
        bad = [x for x in v if not rx.match(x)]
        if bad:
            raise CapError("invalid", f"{name} must match {rx.pattern}: {bad}")
    return list(dict.fromkeys(v))


def _check_body(ctx: Ctx, b: dict) -> dict:
    kind = b.get("kind")
    trow = ctx.type(kind) if isinstance(kind, str) else None
    if trow is None or trow.get("kind_of") != "item":
        raise CapError("invalid", f"kind '{kind}' is not an item type of this namespace's manifest")
    if not ctx.type_may(trow, "write"):
        raise CapError("forbidden", f"role '{ctx.role}' may not write items of type '{kind}'")
    text = b.get("text")
    if not isinstance(text, str) or not text.strip():
        raise CapError("invalid", "text is required")
    if len(text) > TEXT_MAX:
        raise CapError("invalid", f"text is longer than {TEXT_MAX} characters")
    title = b.get("title") or ""
    if not isinstance(title, str) or len(title) > title_chars():
        raise CapError("invalid", f"title must be a string of at most {title_chars()} characters")
    attrs = b.get("attrs") or {}
    if not isinstance(attrs, dict):
        raise CapError("invalid", "attrs must be an object")
    errs = core.validate(ctx.st.schema_for(trow), attrs)
    if errs:
        raise CapError("validation_failed", "attrs do not match the type schema", extra={"errors": errs})
    guards = _str_list(b.get("guard_names"), "guard_names", GUARDS_MAX)
    gm = b.get("guard_metric")
    if gm is not None and (not isinstance(gm, str) or not gm or len(gm) > 256):
        raise CapError("invalid", "guard_metric must be a series name")
    md = b.get("metric_dir")
    if md is not None and md not in ("up", "down"):
        raise CapError("invalid", "metric_dir must be up or down")
    prov = b.get("provenance") or {}
    if not isinstance(prov, dict):
        raise CapError("invalid", "provenance must be {runs, event_ids}")
    runs = _str_list(prov.get("runs"), "provenance.runs", PROVENANCE_MAX)
    evs = _str_list(prov.get("event_ids"), "provenance.event_ids", PROVENANCE_MAX)
    ttl = core.parse_int(b.get("ttl_days"), "ttl_days", None, TTL_MIN, TTL_MAX)
    bound = b.get("bound") or "project"
    if bound not in BOUNDS:
        raise CapError("invalid", f"bound must be one of {', '.join(BOUNDS)}")
    if bound == "people" and ttl is not None:
        ttl = min(ttl, PEOPLE_TTL_MAX)
    triggers = _str_list(b.get("triggers"), "triggers", TRIGGERS_MAX, TRIGGER_RE)
    if b.get("updates") and b.get("contradicts"):
        raise CapError("invalid", "give updates or contradicts, not both")
    key, values, ksha, _ = key_of(ctx.st, b.get("keyspace"), b.get("key"))
    project = ctx.require_project(b.get("project"))
    return {"trow": trow, "kind": kind, "text": text.strip(), "title": title.strip(), "attrs": attrs,
            "guards": guards, "guard_metric": gm, "metric_dir": md, "runs": runs, "event_ids": evs, "ttl": ttl,
            "bound": bound, "triggers": triggers, "keyspace": b["keyspace"], "key": key, "key_sha": ksha,
            "project": project, "updates": b.get("updates"), "contradicts": b.get("contradicts")}


def _near_dup(ctx: Ctx, d: dict) -> str | None:
    """A live item of the same key_sha+kind whose text means the same (cosine >= KH_DISTILL_SAME)."""
    with sqldb.connect() as c:
        rows = c.execute("SELECT item_id, title, text, text_sha, project, scope FROM kh_item WHERE ns=? AND "
                         "key_sha=? AND kind=? AND status IN ('proposed','active','contested') "
                         f"ORDER BY updated_ms DESC LIMIT {NEAR_DUP_POOL}", (ctx.ns, d["key_sha"], d["kind"])).fetchall()
    tsha = text_sha(d["text"])
    cands = [_dict(r) for r in rows if r["text_sha"] != tsha and visible(ctx, _dict(r))]
    if not cands:
        return None
    try:
        import numpy as np

        emb = _embedder()
        q = emb.encode(d["text"], normalize_embeddings=True)
        m = emb.encode([r["text"] or "" for r in cands], normalize_embeddings=True)
        scores = np.asarray(m) @ np.asarray(q)
    except Exception:  # noqa: BLE001
        return None
    best = int(scores.argmax())
    return cands[best]["item_id"] if float(scores[best]) >= same_floor() else None


def _new_id(c, ns: str, kind: str, ksha: str, tsha: str) -> str:
    base = "it_" + core.sha256_text(core.canonical([ns, kind, ksha, tsha]))[:24]
    if _read(c, ns, base) is None:
        return base
    return f"{base}_{os.urandom(3).hex()}"


def propose(ctx: Ctx, request: Request | None, body: dict) -> tuple[int, dict]:
    ctx.require("items", "propose")
    body = core.apply_body_filters(ctx, "items", body)
    d = _check_body(ctx, body)
    from app import distill

    leaked = distill.leak(f"{d['title']}\n{d['text']}", d["project"] or None)
    if leaked:
        raise CapError("leak_refused", "the item quotes something that must not be stored in shared knowledge",
                       extra={"match": leaked})
    lint_errs = lint(ctx.st, d["text"], d["guards"])
    tsha = text_sha(d["text"])
    near = None if (d["updates"] or d["contradicts"]) else _near_dup(ctx, d)
    runs = d["runs"] or [ctx.run_id or ""]
    events: list[tuple] = []
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        if d["updates"]:
            out = _update(c, ctx, request, d, tsha, now)
            status, result = 201, "updated"
            row = out
            extra: dict = {}
        else:
            dup = near
            if dup is None:
                r = c.execute("SELECT item_id, project, scope FROM kh_item WHERE ns=? AND key_sha=? AND kind=? AND "
                              "text_sha=? AND status IN ('proposed','active','contested')",
                              (ctx.ns, d["key_sha"], d["kind"], tsha)).fetchall()
                vis = [x for x in r if visible(ctx, _dict(x))]
                dup = vis[0]["item_id"] if vis else None
            if dup is not None and not d["contradicts"]:
                cur = _dict(_read(c, ctx.ns, dup))
                sup = _add_support(c, ctx, cur, d["project"], runs, d["event_ids"])
                row = sup["row"]
                extra = {"with": dup, "promoted": sup["promoted"]}
                if sup["promoted"]:
                    events.append(("kh.item_promoted", {"item_id": dup, "scope": "cross"}))
                if sup["auto_active"]:
                    events.append(("kh.item_status", {"item_id": dup, "from": "proposed", "to": "active",
                                                      "reason": "auto_after_evidence"}))
                status, result = 200, "merged"
            else:
                target = None
                if d["contradicts"]:
                    target = _dict(_get_visible(c, ctx, str(d["contradicts"])))
                    if target["status"] not in LIVE:
                        raise CapError("invalid", f"item '{target['item_id']}' is not live")
                iid = _new_id(c, ctx.ns, d["kind"], d["key_sha"], tsha)
                st0 = "contested" if target else "proposed"
                row0 = {"ns": ctx.ns, "item_id": iid, "kind": d["kind"], "keyspace": d["keyspace"],
                        "key_json": core.dumps(d["key"]), "key_sha": d["key_sha"], "project": d["project"],
                        "scope": "project", "status": st0,
                        "status_reason": f"contradicts {target['item_id']}" if target else None,
                        "title": d["title"], "text": d["text"], "text_sha": tsha, "attrs_json": core.dumps(d["attrs"]),
                        "provenance_json": core.dumps({"runs": [], "event_ids": []}), "evidence_count": 0,
                        "support_projects_json": "[]", "guard_names_json": core.dumps(d["guards"]),
                        "guard_metric": d["guard_metric"], "metric_dir": d["metric_dir"], "ttl_days": d["ttl"],
                        "expires_ms": None, "activated_ms": None, "last_confirmed_ms": None, "superseded_by": None,
                        "actor_json": ctx.actor_json(), "rev": 1, "vec_rev": 0, "created_ms": now,
                        "updated_ms": now, "bound": d["bound"], "triggers_json": core.dumps(d["triggers"])}
                sqldb.insert_new(c, "kh_item", row0)
                sup = _add_support(c, ctx, row0, d["project"], runs, d["event_ids"])
                row = sup["row"]
                extra = {"promoted": sup["promoted"]}
                if sup["promoted"]:
                    events.append(("kh.item_promoted", {"item_id": iid, "scope": "cross"}))
                if target:
                    if target["status"] != "contested":
                        c.execute("UPDATE kh_item SET status='contested', status_reason=?, rev=rev+1, updated_ms=? "
                                  "WHERE ns=? AND item_id=?", (f"contradicted by {iid}", now, ctx.ns,
                                                               target["item_id"]))
                        events.append(("kh.item_status", {"item_id": target["item_id"], "from": target["status"],
                                                          "to": "contested", "reason": f"contradicted by {iid}"}))
                    c.execute("DELETE FROM kh_item_confirm WHERE ns=? AND item_id=?", (ctx.ns, target["item_id"]))
                    extra["with"] = target["item_id"]
                status, result = 201, ("contested" if target else "accepted")
    ids = [row["item_id"]] + ([extra["with"]] if extra.get("with") else [])
    project_ids(ctx.ns, ids)
    for kind, ev in events:
        core.emit(ctx.ns, kind, ev, ctx, project=d["project"])
    out = {"result": result, "item": view(row), "lint": lint_errs, **{k: v for k, v in extra.items() if v is not None}}
    return status, out


def _update(c, ctx: Ctx, request: Request | None, d: dict, tsha: str, now: int) -> dict:
    cur = _dict(_get_visible(c, ctx, str(d["updates"])))
    if cur["status"] not in LIVE:
        raise CapError("invalid", f"item '{cur['item_id']}' is not live")
    if cur["kind"] != d["kind"] or cur["key_sha"] != d["key_sha"]:
        raise CapError("invalid", "updates must keep the item's kind, keyspace and key")
    expected = core.require_if_match(request, bool(d["trow"].get("require_if_match"))) if request is not None else None
    sets = {"title": d["title"], "text": d["text"], "text_sha": tsha, "attrs_json": core.dumps(d["attrs"]),
            "guard_names_json": core.dumps(d["guards"]), "guard_metric": d["guard_metric"],
            "metric_dir": d["metric_dir"], "triggers_json": core.dumps(d["triggers"]), "actor_json": ctx.actor_json(),
            "updated_ms": now}
    if d["ttl"] is not None:
        sets["ttl_days"] = d["ttl"]
    core.cas_update(c, "kh_item", {"ns": ctx.ns, "item_id": cur["item_id"]}, sets, expected, ns=ctx.ns,
                    what=f"item {cur['item_id']}")
    row = _dict(_read(c, ctx.ns, cur["item_id"]))
    sup = _add_support(c, ctx, row, d["project"], d["runs"] or [ctx.run_id or ""], d["event_ids"])
    return sup["row"]


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def transition(ctx: Ctx, item_id: str, to: str, reason: str | None = None, superseded_by: str | None = None,
               expected_rev: int | None = None, enforce: bool = True) -> tuple[int, dict]:
    """Status change. -> (200, item view) or (202, {pending}). `enforce=False` skips the role check (KH jobs)."""
    to = "proposed" if to == "unproven" else to
    if to not in STATUSES:
        raise CapError("invalid", f"status must be one of {', '.join(STATUSES)} (or unproven)")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
        raise CapError("invalid", "reason must be a string of at most 500 characters")
    events = []
    with sqldb.connect() as c:
        cur = _dict(_get_visible(c, ctx, item_id))
        frm = cur["status"]
        if to not in TRANSITIONS.get(frm, set()):
            raise CapError("invalid_transition", f"item '{item_id}' cannot go from {frm} to {to}",
                           extra={"from": frm, "to": to})
        trow = ctx.st.type(cur["kind"])
        policy = policy_of(ctx.st, trow)
        if enforce and not may_transition(ctx, policy, to):
            raise CapError("forbidden", f"role '{ctx.role}' may not set items to {to}")
        if expected_rev is not None and expected_rev >= 0 and expected_rev != int(cur["rev"]):
            raise core.write_conflict(ctx.ns, f"item {item_id}", int(cur["rev"]), expected_rev)
        now = sqldb.now_ms(c)
        sets: dict = {"status": to, "status_reason": reason, "updated_ms": now}
        if to == "active":
            errs = lint(ctx.st, cur["text"] or "", core.loads(cur["guard_names_json"], []), c)
            if errs:
                raise CapError("lint_failed", "the item does not pass lint", extra={"errors": errs})
            quorum = policy.get("quorum") or 1
            try:
                quorum = max(1, int(quorum))
            except (TypeError, ValueError):
                quorum = 1
            if quorum > 1 and not ctx.is_admin:
                sqldb.insert_new(c, "kh_item_confirm", {"ns": ctx.ns, "item_id": item_id, "principal": ctx.principal,
                                                        "at_ms": now})
                n = int(c.execute("SELECT COUNT(*) FROM kh_item_confirm WHERE ns=? AND item_id=?",
                                  (ctx.ns, item_id)).fetchone()[0])
                if n < quorum:
                    return 202, {"item_id": item_id, "pending": {"confirmations": n, "quorum": quorum}}
            sets = {**_activate_sets(ctx.st, cur, now, reason or "confirmed")}
            c.execute("DELETE FROM kh_item_confirm WHERE ns=? AND item_id=?", (ctx.ns, item_id))
        elif to == "superseded":
            other = superseded_by
            if not isinstance(other, str) or not other or other == item_id:
                raise CapError("invalid", "superseded needs superseded_by naming another live item")
            o = _read(c, ctx.ns, other)
            if o is None or o["status"] not in LIVE:
                raise CapError("invalid", f"superseded_by '{other}' is not a live item")
            sets["superseded_by"] = other
        elif to == "contested":
            c.execute("DELETE FROM kh_item_confirm WHERE ns=? AND item_id=?", (ctx.ns, item_id))
        if to != "superseded" and frm == "superseded":
            sets["superseded_by"] = None
        sets["actor_json"] = ctx.actor_json()
        cols = list(sets)
        upd = c.execute(f"UPDATE kh_item SET {', '.join(f'{k}=?' for k in cols)}, rev=rev+1 "
                        "WHERE ns=? AND item_id=? AND rev=? AND status=?",
                        [*sets.values(), ctx.ns, item_id, int(cur["rev"]), frm])
        if not upd.rowcount or upd.rowcount <= 0:
            r = _read(c, ctx.ns, item_id)
            raise core.write_conflict(ctx.ns, f"item {item_id}", int(r["rev"]) if r else None, int(cur["rev"]))
        row = _dict(_read(c, ctx.ns, item_id))
        events.append({"item_id": item_id, "from": frm, "to": to, "reason": sets.get("status_reason")})
    project_ids(ctx.ns, [item_id])
    for ev in events:
        core.emit(ctx.ns, "kh.item_status", ev, ctx, project=row["project"])
    return 200, view(row)


def rollback_transition(c, ns: str, item_id: str, reason: str = "auto_rollback") -> bool:
    """metrics rollback, inside the caller's transaction: active -> contested -> withdrawn (conditional on the
    item still being active). The vector projection follows via the items_reindex cron (vec_rev != rev)."""
    now = sqldb.now_ms(c)
    cur = c.execute("UPDATE kh_item SET status='contested', status_reason=?, rev=rev+1, updated_ms=? "
                    "WHERE ns=? AND item_id=? AND status='active'", (reason, now, ns, item_id))
    if not cur.rowcount or cur.rowcount <= 0:
        return False
    c.execute("UPDATE kh_item SET status='withdrawn', status_reason=?, rev=rev+1, updated_ms=? "
              "WHERE ns=? AND item_id=? AND status='contested'", (reason, now, ns, item_id))
    c.execute("DELETE FROM kh_item_confirm WHERE ns=? AND item_id=?", (ns, item_id))
    return True


def support(ctx: Ctx, item_id: str, body: dict) -> dict:
    if not (ctx.allows("items", "propose") or ctx.allows("items", "write")):
        ctx.require("items", "propose")
    project = ctx.require_project(body.get("project"))
    run = body.get("run_id") or ctx.run_id or ""
    if not isinstance(run, str) or len(run) > 256:
        raise CapError("invalid", "run_id must be a string of at most 256 characters")
    evs = _str_list(body.get("event_ids"), "event_ids", PROVENANCE_MAX)
    events = []
    with sqldb.connect() as c:
        cur = _dict(_get_visible(c, ctx, item_id))
        if cur["status"] not in LIVE:
            raise CapError("invalid", f"item '{item_id}' is not live")
        sup = _add_support(c, ctx, cur, project, [run], evs)
    if sup["promoted"]:
        events.append(("kh.item_promoted", {"item_id": item_id, "scope": "cross"}))
    if sup["auto_active"]:
        events.append(("kh.item_status", {"item_id": item_id, "from": "proposed", "to": "active",
                                          "reason": "auto_after_evidence"}))
    project_ids(ctx.ns, [item_id])
    for kind, ev in events:
        core.emit(ctx.ns, kind, ev, ctx, project=project)
    return {"item": view(sup["row"]), "new_support": sup["new_support"], "promoted": sup["promoted"],
            "auto_active": sup["auto_active"]}


def expire(ns: str | None = None) -> list[dict]:
    """Active items past expires_ms -> withdrawn (reason expired). Returns [{ns, item_id}]."""
    out = []
    with sqldb.connect() as c:
        if not sqldb.table_exists(c, "kh_item"):
            return out
        now = sqldb.now_ms(c)
        q = "SELECT ns, item_id, rev, project FROM kh_item WHERE status='active' AND expires_ms IS NOT NULL AND expires_ms<?"
        p: list = [now]
        if ns:
            q += " AND ns=?"
            p.append(ns)
        for r in c.execute(q, p).fetchall():
            upd = c.execute("UPDATE kh_item SET status='withdrawn', status_reason='expired', rev=rev+1, updated_ms=? "
                            "WHERE ns=? AND item_id=? AND rev=? AND status='active'", (now, r[0], r[1], int(r[2])))
            if upd.rowcount and upd.rowcount > 0:
                out.append({"ns": r[0], "item_id": r[1], "project": r[3]})
    by_ns: dict[str, list] = {}
    for x in out:
        by_ns.setdefault(x["ns"], []).append(x["item_id"])
    for k, ids in by_ns.items():
        project_ids(k, ids)
    for x in out:
        core.emit(x["ns"], "kh.item_status", {"item_id": x["item_id"], "from": "active", "to": "withdrawn",
                                              "reason": "expired"}, project=x["project"])
    return out


def _cron_expire() -> int:
    return len(expire(None))


def _cron_reindex() -> int:
    return reindex(None)


CRONS = [("items_expire", 3600, _cron_expire), ("items_reindex", 300, _cron_reindex)]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _vis_sql(ctx: Ctx) -> tuple[str, list]:
    eff = ctx.projects()
    if "*" in eff:
        return "", []
    if not eff:
        return " AND (scope='cross' OR project='')", []
    return f" AND (scope='cross' OR project='' OR project IN ({', '.join('?' for _ in eff)}))", list(eff)


def list_items(ctx: Ctx, q: dict) -> dict:
    sql = "SELECT * FROM kh_item WHERE ns=?"
    p: list = [ctx.ns]
    if q.get("keyspace"):
        sql += " AND keyspace=?"
        p.append(q["keyspace"])
        if q.get("key") is not None:
            _, _, ksha, _ = key_of(ctx.st, q["keyspace"], q["key"])
            sql += " AND key_sha=?"
            p.append(ksha)
    elif q.get("key") is not None:
        raise CapError("invalid", "key needs keyspace")
    sts = ["proposed" if s == "unproven" else s for s in core.csv(q.get("status"))]
    bad = [s for s in sts if s not in STATUSES]
    if bad:
        raise CapError("invalid", f"unknown status {bad}")
    if sts:
        sql += f" AND status IN ({', '.join('?' for _ in sts)})"
        p += sts
    for f in ("kind", "project", "scope"):
        if q.get(f):
            sql += f" AND {f}=?"
            p.append(q[f])
    vs, vp = _vis_sql(ctx)
    sql += vs
    p += vp
    sql += f" ORDER BY created_ms DESC, item_id LIMIT {int(q['limit'])} OFFSET {int(q['offset'])}"
    with sqldb.connect() as c:
        rows = c.execute(sql, p).fetchall()
    return {"items": [view(r) for r in rows], "limit": q["limit"], "offset": q["offset"]}


def get_item(ctx: Ctx, item_id: str) -> dict:
    with sqldb.connect() as c:
        return view(_get_visible(c, ctx, item_id))


def patterns(ctx: Ctx, keyspace: str | None, project: str | None) -> dict:
    sql = "SELECT keyspace, key_json, key_sha, kind, status, scope, evidence_count FROM kh_item WHERE ns=?"
    p: list = [ctx.ns]
    if keyspace:
        sql += " AND keyspace=?"
        p.append(keyspace)
    if project:
        sql += " AND project=?"
        p.append(project)
    vs, vp = _vis_sql(ctx)
    with sqldb.connect() as c:
        rows = c.execute(sql + vs, p + vp).fetchall()
    groups: dict[tuple, dict] = {}
    for r in rows:
        g = groups.setdefault((r[0], r[2], r[3]), {"keyspace": r[0], "key": core.loads(r[1], {}), "key_sha": r[2],
                                                  "kind": r[3], "count": 0, "by_status": {}, "cross": 0,
                                                  "evidence": 0})
        g["count"] += 1
        g["by_status"][r[4]] = g["by_status"].get(r[4], 0) + 1
        g["cross"] += 1 if r[5] == "cross" else 0
        g["evidence"] += int(r[6] or 0)
    out = sorted(groups.values(), key=lambda g: (-g["count"], g["keyspace"], g["key_sha"], g["kind"]))
    return {"patterns": out}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _admin_or_orchestrator(ctx: Ctx) -> None:
    if not (ctx.is_admin or ctx.role == "orchestrator"):
        raise CapError("forbidden", "namespace admin or orchestrator only")


def attach(app: FastAPI) -> None:
    tag = ["v1 items"]

    @app.get("/v1/{ns}/items:patterns", tags=tag, summary="Item counts per (keyspace, key, kind)")
    async def get_patterns(ns: str, keyspace: str | None = Query(None), project: str | None = Query(None),
                           ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require("items", "read")
        return await core.run(patterns, ctx, keyspace, project)

    @app.post("/v1/{ns}/items:lint", tags=tag, summary="Lint an item text")
    async def post_lint(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        body = await core.read_object(request)
        text = body.get("text")
        if not isinstance(text, str):
            raise CapError("invalid", "text must be a string")
        guards = _str_list(body.get("guard_names"), "guard_names", GUARDS_MAX)
        errs = await core.run(lint, ctx.st, text, guards)
        return {"ok": not errs, "errors": errs}

    @app.post("/v1/{ns}/items:expire", tags=tag, summary="Withdraw expired active items now")
    @core.idempotent
    async def post_expire(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        _admin_or_orchestrator(ctx)
        out = await core.run(expire, ctx.ns)
        return core.reply({"expired": len(out), "item_ids": [x["item_id"] for x in out]})

    @app.post("/v1/{ns}/items:reindex", tags=tag, summary="Re-embed items (stale rows, or all)")
    @core.idempotent
    async def post_reindex(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        _admin_or_orchestrator(ctx)
        body = await core.read_object(request)
        project = body.get("project")
        if project is not None and not isinstance(project, str):
            raise CapError("invalid", "project must be a string")
        n = await core.run(reindex, ctx.ns, project or None, bool(body.get("all")), 100_000)
        return core.reply({"reindexed": n})

    @app.post("/v1/{ns}/items/{item_id}:support", tags=tag, summary="Add support (evidence) to an item")
    @core.idempotent
    async def post_support(request: Request, ns: str, item_id: str, ctx: Ctx = Depends(core.ns_ctx)):
        body = await core.read_object(request)
        return core.reply(await core.run(support, ctx, item_id, body))

    @app.post("/v1/{ns}/items", tags=tag, summary="Propose an item")
    @core.idempotent
    async def post_item(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        body = await core.read_object(request)
        status, out = await core.run(propose, ctx, request, body)
        return core.reply(out, status=status, etag_rev=out["item"]["rev"])

    @app.get("/v1/{ns}/items", tags=tag, summary="List items")
    async def get_items(ns: str, keyspace: str | None = Query(None), key: str | None = Query(None),
                        status: str | None = Query(None), kind: str | None = Query(None),
                        project: str | None = Query(None), scope: str | None = Query(None),
                        limit: str | None = Query(None), offset: str | None = Query(None),
                        ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require("items", "read")
        q = {"keyspace": keyspace, "key": key, "status": status, "kind": kind, "project": project, "scope": scope,
             "limit": core.clamp_limit(limit, 100, LIST_MAX),
             "offset": core.parse_int(offset, "offset", 0, 0)}
        return await core.run(list_items, ctx, q)

    @app.get("/v1/{ns}/items/{item_id}", tags=tag, summary="Read one item")
    async def get_one(ns: str, item_id: str, ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require("items", "read")
        out = await core.run(get_item, ctx, item_id)
        return core.reply(out, etag_rev=out["rev"])

    @app.patch("/v1/{ns}/items/{item_id}", tags=tag, summary="Change an item's status")
    async def patch_item(request: Request, ns: str, item_id: str, ctx: Ctx = Depends(core.ns_ctx)):
        body = await core.read_object(request)
        status, out = await core.run(_patch, ctx, item_id, body, core.if_match(request))
        return core.reply(out, status=status, etag_rev=out.get("rev"))


def _patch(ctx: Ctx, item_id: str, body: dict, expected: int | None) -> tuple[int, dict]:
    with sqldb.connect() as c:
        r = _read(c, ctx.ns, item_id)
    trow = ctx.st.type(r["kind"]) if r is not None else None
    if trow is not None and trow.get("require_if_match") and expected is None:
        raise CapError("precondition_required", "send If-Match with the current revision (ETag)")
    return transition(ctx, item_id, body.get("status"), body.get("reason"), body.get("superseded_by"), expected)
