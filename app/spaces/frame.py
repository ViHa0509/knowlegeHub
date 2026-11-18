"""The v2 space frame of KH 7.x over the rebuild's legacy stores.

KH 7.1 gives every project a "space" with named scopes (about, policies, feature-graph, knowledges, logs,
findings, lesson-learns, plus system scopes). Old projects keep their data in place ("legacy frame"): each
scope is backed by the collection the v6.5 layout already uses. This module is that mapping and the row
shape the space routes answer (observed on the KH 7.1.0 build, see docs/ref71/).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from app import config
from app import vector_store as vs

# (scope, store, legacy read scope, alias, system). "legacy read scope" is the scope name vs.read_lanes knows.
SCOPES: list[dict[str, Any]] = [
    {"scope": "about", "store": "sqlite", "legacy": "about", "alias": None, "system": False},
    {"scope": "policies", "store": "sqlite", "legacy": None, "alias": None, "system": False},
    {"scope": "feature-graph", "store": "graph", "legacy": None, "alias": "feature_graph", "system": False},
    {"scope": "knowledges", "store": "vector", "legacy": "knowledge", "alias": "knowledge", "system": False},
    {"scope": "logs", "store": "event", "legacy": "log", "alias": "log", "system": False},
    {"scope": "findings", "store": "vector", "legacy": "findings", "alias": None, "system": False},
    {"scope": "lesson-learns", "store": "vector", "legacy": "lesson", "alias": "lesson", "system": False},
    {"scope": "members", "store": "sqlite", "legacy": None, "alias": None, "system": True},
    {"scope": "links", "store": "graph", "legacy": None, "alias": None, "system": True},
    {"scope": "index", "store": "sqlite", "legacy": None, "alias": None, "system": True},
    {"scope": "cases", "store": "vector", "legacy": None, "alias": None, "system": True},
]
_BY_NAME = {s["scope"]: s for s in SCOPES}
for _s in SCOPES:
    if _s["alias"]:
        _BY_NAME.setdefault(_s["alias"], _s)
_BY_NAME.setdefault("lessons", _BY_NAME["lesson-learns"])
_BY_NAME.setdefault("policy", _BY_NAME["policies"])

# Scopes a map / search reads by meaning (vector rows).
CONTENT_SCOPES = ("knowledges", "findings", "lesson-learns", "logs")


def scope_of(name: str) -> Optional[dict]:
    return _BY_NAME.get((name or "").strip().lower())


def backing(space: str, s: dict) -> str:
    name = s["scope"]
    if name == "about":
        return f"legacy:kh_about_{space}"
    if name == "policies":
        return f"legacy:kernel_rule tier={space}"
    if name == "feature-graph":
        return "sqlite graph"
    if name == "logs":
        return f"legacy:{vs.part_collection_name('log', space)}+{config.HOTLOG_COLLECTION}"
    if name == "members":
        return "kh_project_member" if not config.AUTH_ISSUER else "identity service"
    if name == "links":
        return "row metadata links"
    if name == "index":
        return "row metadata keys"
    if name == "cases":
        return f"kh_cases_{space}"
    return "legacy:" + vs.part_collection_name(s["legacy"], space)


def qualify(space: str, scope: str, row_id: str) -> str:
    return f"{space}/{scope}/{row_id}"


def split_qid(qid: str) -> tuple[Optional[str], Optional[str], str]:
    """'space/scope/id' -> (space, scope, id); 'scope/id' -> (None, scope, id); 'id' -> (None, None, id)."""
    parts = (qid or "").strip().split("/")
    if len(parts) >= 3:
        return parts[0], parts[1], "/".join(parts[2:])
    if len(parts) == 2 and scope_of(parts[0]):
        return None, parts[0], parts[1]
    return None, None, qid.strip()


def collections_of(space: str, s: dict) -> list:
    """Existing collection handles behind a content scope (never creates)."""
    legacy = s.get("legacy")
    if not legacy or s["store"] not in ("vector", "event"):
        return []
    out = []
    for name in {vs.part_collection_name(legacy, space), vs.scope_collection_name(legacy, space)}:
        if name:
            h = vs.get_existing(name)
            if h is not None:
                out.append(h)
    if s["scope"] == "logs":
        h = vs.get_existing(config.HOTLOG_COLLECTION)
        if h is not None:
            out.append(("hot", h))
    return out


def lanes(space: str, s: dict) -> list[tuple[Any, Optional[dict]]]:
    """[(handle, where)] for one scope of one space. Lessons of a non-personal space also include knowledge
    rows of kind lesson (KH keeps lesson-learned's lessons in its knowledges)."""
    out: list[tuple[Any, Optional[dict]]] = []
    for h in collections_of(space, s):
        if isinstance(h, tuple):
            out.append((h[1], {"$and": [{"project": {"$eq": space}}, {"scope": {"$eq": s["legacy"]}}]}))
        else:
            out.append((h, None))
    return out


def count(space: str, s: dict) -> int:
    n = 0
    for h, where in lanes(space, s):
        try:
            n += len(vs.meta_rows_vt(h, where)) if where else int(h.count())
        except Exception:  # noqa: BLE001
            continue
    if s["scope"] == "policies":
        try:
            from app.kernel import R

            n = len(R.list_rules(space))
        except Exception:  # noqa: BLE001
            n = 0
    if s["scope"] == "about":
        n = 1 if about_row(space) else 0
    return n


def about_row(space: str) -> Optional[dict]:
    h = vs.get_existing(vs.part_collection_name("about", space))
    if h is None:
        return None
    rows = vs.get_vt(h, limit=1)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Row shape
# ---------------------------------------------------------------------------


def _jl(v) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip().startswith("["):
        try:
            x = json.loads(v)
            return x if isinstance(x, list) else []
        except ValueError:
            return []
    if isinstance(v, str) and v.strip():
        return [x.strip() for x in v.split(",") if x.strip()]
    return []


def _jd(v) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str) and v.strip().startswith("{"):
        try:
            x = json.loads(v)
            return x if isinstance(x, dict) else {}
        except ValueError:
            return {}
    return {}


def first_line(text: str, n: int = 200) -> str:
    from app.ctx_util import short

    line = (text or "").strip().split("\n", 1)[0]
    return short(line, n)


def row_name(meta: dict, text: str) -> str:
    for k in ("name", "subject", "title", "key", "topic"):
        v = meta.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return first_line(text, 60)


def row_type(meta: dict, s: dict) -> str:
    for k in ("type", "kind"):
        v = meta.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return {"knowledges": "knowledge", "logs": "log", "findings": "finding", "lesson-learns": "lesson"}.get(
        s["scope"], s["scope"])


def row_core(meta: dict, text: str) -> list[str]:
    core = _jl(meta.get("core"))
    if core:
        return [str(x) for x in core]
    lines = [ln.strip(" -\t") for ln in (text or "").split("\n") if ln.strip()]
    return lines[1:6] if len(lines) > 1 else lines[:1]


def row_out(space: str, s: dict, hit: dict, full: bool = True) -> dict:
    """The row shape of GET /spaces/{space}/scopes/{scope}/rows (KH 7.1)."""
    meta = dict(hit.get("metadata") or {})
    text = hit.get("source_text") or meta.get("source_text") or meta.get("text") or ""
    rid = hit.get("vector_id") or hit.get("id") or ""
    name = row_name(meta, text)
    out = {
        "id": rid,
        "qid": qualify(space, s["scope"], rid),
        "space": space,
        "scope": s["scope"],
        "type": row_type(meta, s),
        "name": name,
        "summary": meta.get("summary") or first_line(text),
        "where": meta.get("where") or name,
        "core": row_core(meta, text),
        "keys": _jl(meta.get("keys")) or [name],
        "links": _jl(meta.get("links")),
        "sections": _jd(meta.get("sections")),
        "src": _jl(meta.get("src")),
        "ts": meta.get("ts") or meta.get("timestamp") or meta.get("created_at") or "",
    }
    if full:
        out["text"] = text
        for k in ("kind", "project", "author", "topic"):
            if k in meta and k not in out:
                out[k] = meta[k]
    if "score" in hit:
        out["score"] = round(float(hit["score"]), 6)
    return out


def expand_out(row: dict, section: Optional[str] = None) -> dict:
    """The answer of kh_expand (GET /spaces/{space}/rows/{id}), shaped like KH 7.1."""
    secs = row.get("sections") or {}
    body = {
        "id": row["qid"], "type": row["type"], "name": row["name"], "summary": row["summary"],
        "where": row["where"], "core": row["core"], "keys": row["keys"], "links": row["links"],
        "sections": sorted(secs) if secs else (["detail"] if row.get("text") else []),
        "src": row["src"], "history": [],
    }
    if section:
        if section in secs:
            body["section"] = {"name": section, "text": secs[section]}
        elif section == "detail":
            body["section"] = {"name": "detail", "text": row.get("text", "")}
        else:
            body["section"] = None
    else:
        body["text"] = row.get("text", "")
    return body


def find_row(space: str, row_id: str, scope: Optional[str] = None) -> Optional[dict]:
    """Look a row up by id in one scope, or in every content scope of the space."""
    scopes = [scope_of(scope)] if scope else [scope_of(x) for x in CONTENT_SCOPES]
    if scope and scope_of(scope) and scope_of(scope)["scope"] == "about":
        a = about_row(space)
        return row_out(space, scope_of("about"), a) if a and a.get("vector_id") == row_id else None
    for s in scopes:
        if not s:
            continue
        for h, where in lanes(space, s):
            try:
                got = vs.get_ids_vt(h, [row_id], where)
            except Exception:  # noqa: BLE001
                continue
            if got:
                return row_out(space, s, got[0])
    return None
