"""Policy action table storage (spec §3.7.2): policy_actions, policy_rules, policy_usage.

Tiers, most specific first: [caller's personal project, project (unless EVERY), EVERY]. The most specific row
wins per key (action for the menu, sub_action for a plan).
"""
from __future__ import annotations

import logging
from typing import Iterable

from app import sqldb
from app import ctx_config as C
from app import ctx_util as U

log = logging.getLogger(__name__)

EVERY = "every project"

_ACTIONS_SQLITE = """CREATE TABLE IF NOT EXISTS policy_actions (
    project TEXT NOT NULL DEFAULT 'every project', action TEXT NOT NULL, description TEXT,
    source_policy TEXT DEFAULT '', priority INTEGER DEFAULT 100, updated_at TEXT,
    PRIMARY KEY (project, action))"""
_ACTIONS_HANA = """CREATE COLUMN TABLE policy_actions (
    project NVARCHAR(200) DEFAULT 'every project' NOT NULL, action NVARCHAR(200) NOT NULL, description NCLOB,
    source_policy NVARCHAR(200) DEFAULT '', priority INTEGER DEFAULT 100, updated_at NVARCHAR(40),
    PRIMARY KEY (project, action))"""
_RULES_SQLITE = """CREATE TABLE IF NOT EXISTS policy_rules (
    project TEXT NOT NULL DEFAULT 'every project', action TEXT NOT NULL, sub_action TEXT NOT NULL,
    seq INTEGER DEFAULT 0, clauses TEXT DEFAULT '', content TEXT, updated_at TEXT,
    PRIMARY KEY (project, action, sub_action))"""
_RULES_HANA = """CREATE COLUMN TABLE policy_rules (
    project NVARCHAR(200) DEFAULT 'every project' NOT NULL, action NVARCHAR(200) NOT NULL,
    sub_action NVARCHAR(200) NOT NULL, seq INTEGER DEFAULT 0, clauses NCLOB DEFAULT '', content NCLOB,
    updated_at NVARCHAR(40), PRIMARY KEY (project, action, sub_action))"""
_USAGE_SQLITE = """CREATE TABLE IF NOT EXISTS policy_usage (
    project TEXT NOT NULL, action TEXT NOT NULL, fetches INTEGER DEFAULT 0, last_fetch TEXT,
    PRIMARY KEY (project, action))"""
_USAGE_HANA = """CREATE COLUMN TABLE policy_usage (
    project NVARCHAR(200) NOT NULL, action NVARCHAR(200) NOT NULL, fetches INTEGER DEFAULT 0,
    last_fetch NVARCHAR(40), PRIMARY KEY (project, action))"""

_ready = False


def _migrate_legacy(c) -> None:
    """A SQLite table that predates the project column is rebuilt; old rows become EVERY rows."""
    if sqldb.dialect(c) != "sqlite":
        return
    for table, ddl, cols in (("policy_actions", _ACTIONS_SQLITE,
                              "action, description, source_policy, priority, updated_at"),
                             ("policy_rules", _RULES_SQLITE,
                              "action, sub_action, seq, clauses, content, updated_at")):
        if sqldb.table_exists(c, table) and not sqldb.column_exists(c, table, "project"):
            c.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
            c.execute(ddl)
            c.execute(f"INSERT INTO {table} (project, {cols}) SELECT '{EVERY}', {cols} FROM {table}_old")
            c.execute(f"DROP TABLE {table}_old")


def ensure() -> None:
    global _ready
    if _ready:
        return
    with sqldb.connect() as c:
        _migrate_legacy(c)
        sqldb.create_table(c, "policy_actions", _ACTIONS_SQLITE, _ACTIONS_HANA)
        sqldb.create_table(c, "policy_rules", _RULES_SQLITE, _RULES_HANA)
        sqldb.create_index(c, "ix_policy_rules_seq", "policy_rules", ["project", "action", "seq"])
        sqldb.create_table(c, "policy_usage", _USAGE_SQLITE, _USAGE_HANA)
    _ready = True


def _dict(r, cols: Iterable[str]) -> dict:
    return {k: r[i] for i, k in enumerate(cols)}


_A_COLS = ("project", "action", "description", "source_policy", "priority", "updated_at")
_R_COLS = ("project", "action", "sub_action", "seq", "clauses", "content", "updated_at")


def scope(project: str | None) -> list[str]:
    """[personal tier, project (unless EVERY), EVERY], deduped, nulls dropped (`_scope`)."""
    out: list[str] = []
    for t in (U.personal_tier(), project if project != EVERY else None, EVERY):
        if t and t not in out:
            out.append(t)
    return out


def _actions_in(c, tiers: list[str]) -> list[dict]:
    ph = ",".join("?" for _ in tiers)
    rows = c.execute(f"SELECT {', '.join(_A_COLS)} FROM policy_actions WHERE project IN ({ph})", tiers).fetchall()
    return [_dict(r, _A_COLS) for r in rows]


def _rules_in(c, tiers: list[str], action: str | None = None) -> list[dict]:
    ph = ",".join("?" for _ in tiers)
    sql = f"SELECT {', '.join(_R_COLS)} FROM policy_rules WHERE project IN ({ph})"
    params: list = list(tiers)
    if action is not None:
        sql += " AND action=?"
        params.append(action)
    return [_dict(r, _R_COLS) for r in c.execute(sql, params).fetchall()]


def _by_tier(rows: list[dict], tiers: list[str]) -> list[dict]:
    rank = {t: i for i, t in enumerate(tiers)}
    return sorted(rows, key=lambda r: rank.get(r["project"], 99))


def list_actions(project: str | None) -> list[dict]:
    ensure()
    tiers = scope(project)
    with sqldb.connect() as c:
        rows = _actions_in(c, tiers)
    seen: dict[str, dict] = {}
    for r in _by_tier(rows, tiers):
        seen.setdefault(r["action"], r)
    out = list(seen.values())
    for r in out:
        r["priority"] = int(r["priority"] if r["priority"] is not None else 100)
    out.sort(key=lambda r: (-r["priority"], r["action"]))
    return out


def plan(action: str, project: str | None) -> list[dict]:
    ensure()
    tiers = scope(project)
    with sqldb.connect() as c:
        rows = _rules_in(c, tiers, action)
    seen: dict[str, dict] = {}
    for r in _by_tier(rows, tiers):
        seen.setdefault(r["sub_action"], r)
    out = list(seen.values())
    for r in out:
        r["seq"] = int(r["seq"] or 0)
    out.sort(key=lambda r: (r["seq"], r["sub_action"]))
    return out


def get_rule(action: str, sub_action: str, project: str | None) -> dict | None:
    for r in plan(action, project):
        if r["sub_action"] == sub_action:
            return r
    return None


def has_action(action: str, project: str | None) -> bool:
    return any(a["action"] == action for a in list_actions(project))


def get_action(action: str, tier: str) -> dict | None:
    ensure()
    with sqldb.connect() as c:
        r = c.execute(f"SELECT {', '.join(_A_COLS)} FROM policy_actions WHERE project=? AND action=?",
                      (tier, action)).fetchone()
    return _dict(r, _A_COLS) if r else None


def tier_rules(action: str, tier: str) -> list[dict]:
    ensure()
    with sqldb.connect() as c:
        return _rules_in(c, [tier], action)


def menu(project: str | None) -> list[dict]:
    """list_actions, each with `sub_actions` (names in plan order)."""
    out = []
    for a in list_actions(project):
        a = dict(a)
        a["sub_actions"] = [r["sub_action"] for r in plan(a["action"], project)]
        out.append(a)
    return out


def menu_text(project: str | None) -> str:
    lines = []
    for a in menu(project):
        lines.append(f"  - ACTION '{a['action']}' [project: {a['project']}] - when: {a['description'] or ''}")
        lines.append("    sub-actions in order: " + " -> ".join(a["sub_actions"]))
    lines.append("  - If no action fits, answer none and proceed. Never invent a label.")
    return "\n".join(lines)


def set_action(project: str, action: str, description: str, source_policy: str = "", priority: int = 100) -> dict:
    ensure()
    row = {"project": project, "action": action, "description": description,
           "source_policy": source_policy or "", "priority": int(priority), "updated_at": U.now_iso()}
    with sqldb.connect() as c:
        sqldb.upsert(c, "policy_actions", ["project", "action"], row)
    return row


def set_rule(project: str, action: str, sub_action: str, content: str, seq: int = 0, clauses: str = "") -> dict:
    ensure()
    row = {"project": project, "action": action, "sub_action": sub_action, "seq": int(seq),
           "clauses": clauses or "", "content": content, "updated_at": U.now_iso()}
    with sqldb.connect() as c:
        sqldb.upsert(c, "policy_rules", ["project", "action", "sub_action"], row)
    return row


def delete_rule(project: str, action: str, sub_action: str) -> int:
    ensure()
    with sqldb.connect() as c:
        cur = c.execute("DELETE FROM policy_rules WHERE project=? AND action=? AND sub_action=?",
                        (project, action, sub_action))
        return max(0, cur.rowcount or 0)


def delete_action(project: str, action: str, cascade: bool = False) -> dict:
    ensure()
    with sqldb.connect() as c:
        rules = 0
        if cascade:
            rules = c.execute("DELETE FROM policy_rules WHERE project=? AND action=?", (project, action)).rowcount
        cur = c.execute("DELETE FROM policy_actions WHERE project=? AND action=?", (project, action))
        return {"deleted": max(0, cur.rowcount or 0), "rules_deleted": max(0, rules or 0)}


def record_fetch(project: str, action: str) -> None:
    ensure()
    try:
        with sqldb.connect() as c:
            cur = c.execute("UPDATE policy_usage SET fetches=fetches+1, last_fetch=? WHERE project=? AND action=?",
                            (U.now_iso(), project, action))
            if not cur.rowcount:
                if not sqldb.insert_new(c, "policy_usage", {"project": project, "action": action, "fetches": 1,
                                                           "last_fetch": U.now_iso()}):
                    c.execute("UPDATE policy_usage SET fetches=fetches+1, last_fetch=? WHERE project=? AND action=?",
                              (U.now_iso(), project, action))
    except Exception as e:  # noqa: BLE001
        log.warning("policy fetch count failed: %s", e)


def last_fetch(action: str) -> str | None:
    """Latest fetch of an action over all tiers."""
    ensure()
    with sqldb.connect() as c:
        r = c.execute("SELECT MAX(last_fetch) FROM policy_usage WHERE action=?", (action,)).fetchone()
    return r[0] if r else None


def owner_projects() -> list[str]:
    ensure()
    with sqldb.connect() as c:
        a = [r[0] for r in c.execute("SELECT DISTINCT project FROM policy_actions").fetchall()]
        b = [r[0] for r in c.execute("SELECT DISTINCT project FROM policy_rules").fetchall()]
    return sorted(set(a) | set(b))


def owns_rows(project: str) -> bool:
    return project in owner_projects()


def all_actions() -> list[dict]:
    ensure()
    with sqldb.connect() as c:
        return [_dict(r, _A_COLS) for r in
                c.execute(f"SELECT {', '.join(_A_COLS)} FROM policy_actions").fetchall()]


def all_rules() -> list[dict]:
    ensure()
    with sqldb.connect() as c:
        return [_dict(r, _R_COLS) for r in c.execute(f"SELECT {', '.join(_R_COLS)} FROM policy_rules").fetchall()]


def counts() -> dict:
    ensure()
    with sqldb.connect() as c:
        na = c.execute("SELECT COUNT(*) FROM policy_actions").fetchone()[0]
        nr = c.execute("SELECT COUNT(*) FROM policy_rules").fetchone()[0]
        db = sqldb.dialect(c)
    return {"db": db, "actions": int(na), "rules": int(nr)}


def bulk(actions: list[dict], rules: list[dict], replace: bool = False) -> dict:
    ensure()
    now = U.now_iso()
    with sqldb.connect() as c:
        if replace:
            c.execute("DELETE FROM policy_rules")
            c.execute("DELETE FROM policy_actions")
        for a in actions:
            sqldb.upsert(c, "policy_actions", ["project", "action"], {
                "project": a.get("project") or EVERY, "action": a["action"], "description": a.get("description") or "",
                "source_policy": a.get("source_policy") or "", "priority": int(a.get("priority", 100) or 100),
                "updated_at": now})
        for r in rules:
            sqldb.upsert(c, "policy_rules", ["project", "action", "sub_action"], {
                "project": r.get("project") or EVERY, "action": r["action"], "sub_action": r["sub_action"],
                "seq": int(r.get("seq", 0) or 0), "clauses": r.get("clauses") or "", "content": r.get("content") or "",
                "updated_at": now})
    return {"actions": len(actions), "rules": len(rules), "replace": bool(replace)}


def delete_project_rows(pid: str) -> dict:
    ensure()
    with sqldb.connect() as c:
        a = c.execute("DELETE FROM policy_actions WHERE project=?", (pid,)).rowcount
        r = c.execute("DELETE FROM policy_rules WHERE project=?", (pid,)).rowcount
        c.execute("DELETE FROM policy_usage WHERE project=?", (pid,))
    return {"policy_actions_deleted": max(0, a or 0), "policy_rules_deleted": max(0, r or 0)}


def is_empty() -> bool:
    ensure()
    with sqldb.connect() as c:
        return int(c.execute("SELECT COUNT(*) FROM policy_actions").fetchone()[0]) == 0


__all__ = ["EVERY", "C", "ensure", "scope", "list_actions", "plan", "get_rule", "has_action", "menu", "menu_text",
           "set_action", "set_rule", "delete_rule", "delete_action", "record_fetch", "bulk", "owner_projects",
           "counts", "is_empty"]
