"""Seed loader for the context engine (Appendix A): kernel tags, common kernel rules, hook rules, the
every-project action table.

Startup (`seed_all`, from kernel.start / policy_actions.start) inserts only into EMPTY tables (or missing keys),
so rows edited on a running KH are never overwritten. `POST /kernel/seed` (maintainer) calls
`seed_kernel(force=True)`, which re-applies the seed kernel rows by (tier, slot).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from app import config

log = logging.getLogger(__name__)

SEED_DIR = Path(config.ROOT) / "seeds"
KERNEL_RULES = "kernel_rules.seed.json"
KERNEL_TAGS = "kernel_tags.seed.json"
KERNEL_HOOKS = "kernel_hooks.seed.json"
POLICY_ACTIONS = "policy_actions_every_project.json"


def load(name: str) -> dict:
    with open(SEED_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def seed_tags(force: bool = False) -> dict:
    from app.kernel import tags as T

    if not force and T.count() > 0:
        return {"skipped": "kernel_tag not empty"}
    return T.seed(load(KERNEL_TAGS))


def seed_hooks(force: bool = False) -> dict:
    """Hook rules: the kernel seed's hooks list plus the prod hook snapshot. Inserted only when missing."""
    from app import hooks

    if not force and hooks.count() > 0:
        return {"skipped": "hook_rules not empty"}
    items = list(load(KERNEL_RULES).get("hooks") or []) + list(load(KERNEL_HOOKS).get("hooks") or [])
    added = 0
    for h in items:
        if hooks.insert_if_missing(h["hook_type"], h.get("name") or "default", h["content"],
                                   int(h.get("position", 0))):
            added += 1
    return {"added": added}


def seed_kernel(force: bool = False) -> dict:
    """Tags, hooks, header rule, then the common rules (distinct by construction)."""
    from app.kernel import rules as R

    out = {"tags": seed_tags(force), "hooks": seed_hooks(force)}
    data = load(KERNEL_RULES)
    rows = [data["header"]] + list(data.get("rules") or [])
    if not force and R.count(R.COMMON) > 0:
        out["rules"] = {"skipped": "common tier not empty"}
        return out
    added = updated = 0
    slots = [r["slot"] for r in rows]
    for r in rows:
        if force:
            res = R.store_plan({"tier": R.COMMON, "kind": "common", "slot": r["slot"], "title": r["title"],
                                "content": r["content"], "tags": r["tags"], "position": r["position"],
                                "locked": bool(r.get("locked")), "enabled": True, "overrides": "",
                                "reason": "seed: distinct by construction", "guard": None, "updated_by": "seed",
                                "distinct_from": slots})
            if res["result"] == "added":
                added += 1
            else:
                updated += 1
        elif R.insert_seed_row(R.COMMON, r["slot"], r["title"], r["content"], r["tags"], r["position"],
                               bool(r.get("locked")), "seed: distinct by construction"):
            added += 1
    out["rules"] = {"added": added, "updated": updated, "total": len(rows)}
    return out


def seed_policy_actions(force: bool = False) -> dict:
    from app.policy_actions import store

    if not force and not store.is_empty():
        return {"skipped": "policy_actions not empty"}
    data = load(POLICY_ACTIONS)
    return store.bulk(data["actions"], data["rules"], replace=bool(force and data.get("replace")))


def seed_all() -> dict:
    out = {}
    for name, fn in (("kernel", seed_kernel), ("policy_actions", seed_policy_actions)):
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            log.warning("seed %s failed: %s", name, e)
            out[name] = {"error": str(e)}
    return out
