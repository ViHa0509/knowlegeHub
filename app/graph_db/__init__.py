"""Graph store (spec §1.12), minimal. Backend resolved lazily on first use.

    graph_db                  lazy proxy to the default (orchestrator) graph adapter
    get_default_graph()
    get_project_graph(pid)    LRU-cached (KH_MAX_OPEN_PROJECTS), orchestrator pinned
    drop_project_graph(pid)
    bootstrap()               resolve backend + create tables (lifespan step [3/3])
    backend_name()

GRAPH_BACKEND: hana (required) | auto (probe HANA, fall back to SQLite) | sqlite | kuzu (mapped to SQLite;
Kùzu is not part of the rebuild).

Adapter interface: probe, bootstrap, drop, upsert_nodes([{core_id, label, type, attributes, source_text}]),
upsert_edges([{source_core_id, relation, target_core_id, attributes, source_text, confidence}]),
fetch_graph(core_ids, hierarchy_edges, hops) -> (nodes, edges), delete_nodes(ids) -> {nodes_deleted,
edges_deleted}, delete_orphan_documents() -> int, traverse_logic(start_ids, relations, max_depth).
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections import OrderedDict

from app import config

log = logging.getLogger(__name__)

_backend: str | None = None
_default = None
_projects: "OrderedDict[str, object]" = OrderedDict()
_lock = threading.RLock()


def _resolve() -> str:
    global _backend
    if _backend is not None:
        return _backend
    with _lock:
        if _backend is not None:
            return _backend
        want = config.GRAPH_BACKEND
        if want in ("hana", "auto"):
            try:
                from app.graph_db.hana_adapter import HanaAdapter

                HanaAdapter().probe()
                _backend = "hana"
                return _backend
            except Exception as e:  # noqa: BLE001
                if want == "hana":
                    raise RuntimeError(f"GRAPH_BACKEND=hana but HANA is unreachable: {e}") from e
                log.info("graph: HANA not available (%s); using SQLite", e)
        _backend = "sqlite"
        return _backend


def backend_name() -> str:
    return _resolve()


def sqlite_prefix(pid: str) -> str:
    if pid == config.ORCHESTRATOR_ID:
        return config.GRAPH_TABLE_PREFIX
    slug = re.sub("[^A-Z0-9]+", "_", pid.upper()).strip("_")[:40]
    h = hashlib.sha1(pid.encode("utf-8")).hexdigest()[:6].upper()
    return f"KH_P_{slug}_{h}_"


def _make(pid: str):
    if _resolve() == "hana":
        from app.graph_db.hana_adapter import HanaAdapter, project_prefix

        a = HanaAdapter(project_prefix(pid))
    else:
        from app.graph_db.sqlite_adapter import SqliteGraphAdapter

        a = SqliteGraphAdapter(sqlite_prefix(pid))
    a.bootstrap()
    return a


def get_project_graph(pid: str):
    with _lock:
        a = _projects.get(pid)
        if a is not None:
            _projects.move_to_end(pid)
            return a
        a = _make(pid)
        _projects[pid] = a
        cap = max(1, config.MAX_OPEN_PROJECTS)
        while len(_projects) > cap:
            for k in list(_projects):
                if k != config.ORCHESTRATOR_ID:
                    _projects.pop(k)
                    break
            else:
                break
        return a


def get_default_graph():
    global _default
    if _default is None:
        _default = get_project_graph(config.ORCHESTRATOR_ID)
    return _default


def drop_project_graph(pid: str) -> bool:
    if pid == config.ORCHESTRATOR_ID:
        raise ValueError("refusing to drop the orchestrator graph")
    with _lock:
        _projects.pop(pid, None)
    a = _make(pid)
    return bool(a.drop())


def bootstrap():
    return get_default_graph()


def reset() -> None:
    """Tests: forget the resolved backend and open adapters."""
    global _backend, _default
    with _lock:
        _backend = None
        _default = None
        _projects.clear()


class _LazyGraph:
    def __getattr__(self, name):
        return getattr(get_default_graph(), name)


graph_db = _LazyGraph()
