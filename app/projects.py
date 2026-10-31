"""Projects as the unit of physical isolation (spec §1.6, §2.8 data layer).

HTTP routes live in app/projects_routes.py (§2). Public names:
    ORCHESTRATOR, MANAGE_ROLE
    validate_id(pid) -> canonical id (ValueError when invalid)
    bundle_of(pid, role="project") -> naming dict (pure, no I/O)
    get_project(pid) / exists(pid) / list_projects()
    get_or_create_project(pid, role=None, aliases=(), keywords=(), tools=(), descriptor="",
                          conflicts_with=(), source=None) -> entry
    read_counter(pid) / write_counter(pid, obj)
    delete_project(pid, confirm=False) -> report (preview unless confirm)
    on_delete(fn)  register a cleanup hook fn(pid) -> dict|None, run by delete_project (feedback, policy, ...)
    ensure_orchestrator()
    registry_row(entry) -> (id, text, metadata); sync_orchestrator_registry(encode=None) -> {written, unchanged, total}
    fold(text)
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import unicodedata
from typing import Callable, Iterable

from app import config, docstore
from app import projects_store as store

log = logging.getLogger(__name__)

ORCHESTRATOR = config.ORCHESTRATOR_ID
MANAGE_ROLE = "manage"
_BAD_PREFIXES = ("graphify__", "gitnexus__")
REGISTRY_TREE = "c4_project_registry"
REGISTRY_PREFIX = "project_registry__"

_delete_hooks: list[Callable[[str], dict | None]] = []


def validate_id(pid: str | None) -> str:
    p = (pid or "").strip() if isinstance(pid, str) or pid is None else str(pid).strip()
    if not p:
        raise ValueError("project id must not be empty")
    low = p.lower()
    for bad in _BAD_PREFIXES:
        if low.startswith(bad):
            raise ValueError(f"project id must not start with '{bad}'")
    if p in ("kh-meta", "self-info", "meta", "kh_meta"):
        return ORCHESTRATOR
    return p


def bundle_of(project_id: str, role: str = "project") -> dict:
    pid = project_id
    return {
        "project_id": pid,
        "role": role,
        "dir": os.path.join(config.PROJECTS_PATH, pid),
        "counter": os.path.join(config.PROJECTS_PATH, pid, "counter.json"),
        "bundle": os.path.join(config.PROJECTS_PATH, pid, "bundle.json"),
        "graph_dir": os.path.join(config.KUZU_PROJECTS_PATH, pid),
        "vector": f"kh_src_{pid}",
        "knowledge": f"kh_know_{pid}",
        "log": f"kh_log_{pid}",
        "findings": f"kh_find_{pid}",
        "policy": f"kh_policy_{pid}",
    }


def _merge_list(old: Iterable, new: Iterable) -> list:
    out = list(old or [])
    for x in new or []:
        if x not in out:
            out.append(x)
    return out


def get_project(pid: str) -> dict | None:
    return store.get_one(pid)


def exists(pid: str) -> bool:
    return store.get_one(pid) is not None


def list_projects() -> dict[str, dict]:
    return store.load_all()


def read_counter(pid: str) -> dict:
    return docstore.read_json(bundle_of(pid)["counter"], {"request_total": 0, "issues": {}})


def write_counter(pid: str, obj: dict) -> None:
    docstore.write_json(bundle_of(pid)["counter"], obj)


def get_or_create_project(project_id: str, role: str | None = None, aliases: Iterable[str] = (),
                          keywords: Iterable[str] = (), tools: Iterable[str] = (), descriptor: str = "",
                          conflicts_with: Iterable[str] = (), source: str | None = None) -> dict:
    pid = validate_id(project_id)
    if source is not None and len(str(source)) > 64:
        raise ValueError("source must be at most 64 characters")
    old = store.get_one(pid)
    b = bundle_of(pid, role or (old or {}).get("role") or (MANAGE_ROLE if pid == ORCHESTRATOR else "project"))
    if not docstore._use_db():
        os.makedirs(b["dir"], exist_ok=True)
    if not docstore.exists(b["counter"]):
        docstore.write_json(b["counter"], {"request_total": 0, "issues": {}})
    now = store.now_iso()
    entry = {
        "id": pid,
        "role": b["role"],
        "aliases": _merge_list((old or {}).get("aliases"), aliases),
        "keywords": _merge_list((old or {}).get("keywords"), keywords),
        "tools": _merge_list((old or {}).get("tools"), tools),
        "conflicts_with": _merge_list((old or {}).get("conflicts_with"), conflicts_with),
        "descriptor": (descriptor or "").strip() or (old or {}).get("descriptor") or "",
        "paths": {k: b[k] for k in ("dir", "graph_dir", "vector", "knowledge", "log", "findings", "policy")},
        "source": (str(source).strip() if source and str(source).strip() else None)
        or (old or {}).get("source") or "KH",
        "created_at": (old or {}).get("created_at") or now,
        "updated_at": now,
    }
    store.upsert_one(entry)
    docstore.write_json(b["bundle"], entry)
    return entry


def ensure_orchestrator() -> dict:
    return get_or_create_project(ORCHESTRATOR, role=MANAGE_ROLE)


def on_delete(fn: Callable[[str], dict | None]) -> None:
    """Register a cleanup hook run by delete_project(pid, confirm=True). Failures are logged, not fatal."""
    if fn not in _delete_hooks:
        _delete_hooks.append(fn)


def delete_project(pid: str, confirm: bool = False) -> dict:
    from app import hotlog
    from app import vector_store as vs

    pid = (pid or "").strip()
    if not pid:
        raise ValueError("project id must not be empty")
    if pid == ORCHESTRATOR:
        raise ValueError("the orchestrator project cannot be deleted")
    b = bundle_of(pid)
    basic = [b["vector"], b["knowledge"], b["log"], b["findings"], b["policy"]]
    colls = list(dict.fromkeys(basic + vs.project_collections(pid)))
    existing = [n for n in colls if vs.collection_exists(n)]
    if not confirm:
        return {"project": pid, "preview": True, "confirm_required": True, "collections": existing,
                "registered": exists(pid)}
    report: dict = {"project": pid, "preview": False, "collections_dropped": []}
    for n in colls:
        if vs.drop_collection(n):
            report["collections_dropped"].append(n)
    try:
        from app.graph_db import drop_project_graph

        report["graph_dropped"] = bool(drop_project_graph(pid))
    except Exception as e:  # noqa: BLE001
        log.warning("graph drop for %s failed: %s", pid, e)
        report["graph_dropped"] = False
    # hub descriptor, source index row, route shortcut, C4 registry row
    for coll_name, rid in (("kh_orch_hub", pid), (config.COLLECTION_NAME, f"srcidx__{pid}"),
                           ("kh_orch_resolve", f"shortcut__{pid}"),
                           (vs.part_collection_name("knowledge", ORCHESTRATOR), REGISTRY_PREFIX + pid)):
        h = vs.get_existing(coll_name)
        if h is not None:
            try:
                vs.delete_vt([rid], h)
            except Exception as e:  # noqa: BLE001
                log.warning("delete %s from %s failed: %s", rid, coll_name, e)
    report["registry_row_deleted"] = store.delete_one(pid)
    report["hot_rows_deleted"] = hotlog.delete_project_rows(pid)
    report["bundle_deleted"] = docstore.delete_tree(b["dir"])
    if not docstore._use_db():
        shutil.rmtree(b["dir"], ignore_errors=True)
    for fn in list(_delete_hooks):
        try:
            extra = fn(pid)
            if isinstance(extra, dict):
                report.update(extra)
        except Exception as e:  # noqa: BLE001
            log.warning("delete hook %s for %s failed: %s", getattr(fn, "__name__", fn), pid, e)
    return report


# ---------------------------------------------------------------------------
# C4 project registry rows in kh_know_orchestrator
# ---------------------------------------------------------------------------


def fold(text: str) -> str:
    """NFD, drop combining marks, lower, non-alphanumerics -> space, collapse."""
    t = unicodedata.normalize("NFD", text or "")
    t = "".join(ch for ch in t if not unicodedata.combining(ch)).lower()
    t = re.sub(r"[^0-9a-z]+", " ", t)
    return " ".join(t.split())


def registry_row(entry: dict) -> tuple[str, str, dict]:
    pid = entry["id"]
    role = entry.get("role") or "project"
    aliases = list(entry.get("aliases") or [])
    keywords = list(entry.get("keywords") or [])
    conflicts = list(entry.get("conflicts_with") or [])
    desc = (entry.get("descriptor") or "").strip()
    terms = []
    for t in [pid, *aliases, *keywords]:
        f = fold(t)
        if f and f not in terms:
            terms.append(f)
    terms.sort(key=len, reverse=True)
    parts = [f"PROJECT {pid} (role {role})."]
    if desc:
        parts.append(desc)
    if aliases:
        parts.append("Also called: " + ", ".join(aliases) + ".")
    if keywords:
        parts.append("Keywords: " + ", ".join(keywords) + ".")
    if conflicts:
        parts.append("Never blend with: " + ", ".join(conflicts) + ".")
    text = " ".join(parts)
    meta = {
        "scope": "knowledge", "project": ORCHESTRATOR, "kind": "project-registry", "layered": "1",
        "tree_id": REGISTRY_TREE, "lv": 1, "subject_key": pid, "project_id": pid, "role": role,
        "aliases": "|".join(aliases), "keywords": "|".join(keywords), "terms": "|".join(terms),
        "descriptor": desc, "updated_at": entry.get("updated_at") or "",
    }
    return REGISTRY_PREFIX + pid, text, meta


def sync_orchestrator_registry(encode: Callable | None = None) -> dict:
    """Keep the C4 registry rows in kh_know_orchestrator in step with the `projects` table.

    Writes only changed rows and removes rows of projects no longer registered.
    """
    from app import vector_store as vs
    from app.embeddings import get_embedder

    enc = encode or (lambda texts: get_embedder().encode(texts, normalize_embeddings=True))
    entries = store.load_all()
    coll = vs.get_or_create(vs.part_collection_name("knowledge", ORCHESTRATOR))
    have = coll.get(where={"kind": {"$eq": "project-registry"}}, include=["metadatas", "documents"])
    current = {rid: (doc, meta) for rid, doc, meta in zip(have.get("ids") or [], have.get("documents") or [],
                                                         have.get("metadatas") or [])}
    ids, texts, metas = [], [], []
    unchanged = 0
    for pid in sorted(entries):
        rid, text, meta = registry_row(entries[pid])
        old = current.get(rid)
        if old is not None and old[0] == text and all((old[1] or {}).get(k) == v for k, v in meta.items()):
            unchanged += 1
            continue
        ids.append(rid)
        texts.append(text)
        metas.append(meta)
    if ids:
        vecs = enc(texts)
        vs.upsert_rows(coll, ids, list(vecs), texts, metas)
    stale = [rid for rid in current if rid[len(REGISTRY_PREFIX):] not in entries]
    if stale:
        coll.delete(ids=stale)
    return {"written": len(ids), "unchanged": unchanged, "total": len(entries)}


# ---------------------------------------------------------------------------
# Name -> project resolution (§2.10 / §3.10): literal, no embedder
# ---------------------------------------------------------------------------

_RANK = {"id": 0, "alias": 1, "keyword": 2}


def weak_keyword_len() -> int:
    return config._int("KH_PROJECT_MATCH_WEAK_KEYWORD_LEN", 4)


def _split_pipe(v) -> list[str]:
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x).strip()]
    return [x for x in str(v or "").split("|") if x.strip()]


def registry_entries() -> tuple[list[dict], str]:
    """Candidates for matching: C4 registry rows (`source: c4`), else the projects table (`manifest-fallback`)."""
    from app import vector_store as vs

    out: list[dict] = []
    h = vs.get_existing(vs.part_collection_name("knowledge", ORCHESTRATOR))
    if h is not None:
        try:
            for rid, meta in vs.meta_rows_vt(h, {"kind": {"$eq": "project-registry"}}):
                pid = meta.get("project_id") or meta.get("subject_key") or rid[len(REGISTRY_PREFIX):]
                out.append({"id": pid, "role": meta.get("role") or "project",
                            "aliases": _split_pipe(meta.get("aliases")), "keywords": _split_pipe(meta.get("keywords")),
                            "descriptor": meta.get("descriptor") or "", "source": "c4"})
        except Exception as e:  # noqa: BLE001
            log.warning("C4 registry read failed: %s", e)
    if out:
        return out, "c4"
    for pid, e in sorted(store.load_all().items()):
        out.append({"id": pid, "role": e.get("role") or "project", "aliases": list(e.get("aliases") or []),
                    "keywords": list(e.get("keywords") or []), "descriptor": e.get("descriptor") or "",
                    "source": "manifest-fallback"})
    return out, ("manifest-fallback" if out else "empty")


def match_projects(text: str) -> list[dict]:
    """Literal project matches of `text`, filtered to projects the caller may read (sign-in on).

    Each hit: {project, matched_term, matched_on, term_length, role, weak, source}, sorted by
    (-term_length, rank id<alias<keyword, project).
    """
    from app import auth

    entries, _src = registry_entries()
    visible = auth.visible_projects("project.read")
    hay = " " + fold(text) + " "
    weak_len = weak_keyword_len()
    hits = []
    for e in entries:
        pid = e["id"]
        if visible is not None and pid not in visible:
            continue
        terms: list[tuple[str, str]] = []
        seen: set = set()
        for raw, kind in [(pid, "id")] + [(a, "alias") for a in e["aliases"]] + [(k, "keyword") for k in e["keywords"]]:
            f = fold(raw)
            if f and f not in seen:
                seen.add(f)
                terms.append((f, kind))
        best = None
        for term, kind in terms:
            if f" {term} " not in hay:
                continue
            key = (-len(term), _RANK[kind])
            if best is None or key < best[0]:
                best = (key, term, kind)
        if best is None:
            continue
        _k, term, kind = best
        hits.append({"project": pid, "matched_term": term, "matched_on": kind, "term_length": len(term),
                     "role": e.get("role") or "project", "weak": kind == "keyword" and len(term) < weak_len,
                     "source": e.get("source") or "c4"})
    hits.sort(key=lambda h: (-h["term_length"], _RANK[h["matched_on"]], h["project"]))
    return hits


def resolve_from_hits(hits: list[dict]) -> str | None:
    if not hits or hits[0]["weak"]:
        return None
    if len(hits) > 1 and (hits[0]["term_length"], hits[0]["matched_on"]) == \
            (hits[1]["term_length"], hits[1]["matched_on"]):
        return None
    return hits[0]["project"]


def resolve_project_name(text: str) -> str | None:
    """The one project `text` names: None if no hits, the top hit is weak, or the top two tie."""
    return resolve_from_hits(match_projects(text))


def canonical_or_none(x: str | None) -> str | None:
    """Exact existing id, else resolve_project_name(x)."""
    if not x or not str(x).strip():
        return None
    x = str(x).strip()
    if exists(x):
        return x
    return resolve_project_name(x)
