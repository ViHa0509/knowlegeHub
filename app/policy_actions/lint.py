"""Policy lint (spec §3.7.2 "Lint") and the hook-text renderer (`{menu}` placeholder)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from fastapi.responses import JSONResponse

from app import ctx_config as C
from app import ctx_util as U
from app.policy_actions import store

_WORD = re.compile(r"[a-z0-9]+")
STOP_WORDS = frozenset("""
a an and are as at be but by for from has have i if in into is it its of on or so that the their them then there
these they this to was were will with you your we our not no do does did can could should would may might must
any all each every when what which who whom how why where than too very just also only about over under after
before up down out off own same such more most other some one two me my he she his her
""".split())


def words(text: str | None) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if len(w) > 1 and w not in STOP_WORDS}


def jaccard(a: str | None, b: str | None) -> float:
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def menu_lines(project: str | None = None) -> str:
    """Every-project action menu used by `{menu}`: `- '<action>': <_short(description, 160)>`."""
    return "\n".join(f"- '{a['action']}': {U.short(a.get('description'), C.TURN_WHEN_CHARS)}"
                     for a in store.list_actions(project or store.EVERY))


def render(text: str | None) -> str:
    text = text or ""
    if C.MENU_PLACEHOLDER in text:
        text = text.replace(C.MENU_PLACEHOLDER, menu_lines())
    return text


def check_action(action: str, description: str, project: str) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    if len(description or "") > C.POLICY_DESC_MAX:
        errors.append(f"description of '{action}' is {len(description)} chars; the limit is {C.POLICY_DESC_MAX}")
    for a in store.list_actions(project):
        if a["action"] == action:
            continue
        j = jaccard(description, a.get("description"))
        if j >= C.POLICY_OVERLAP:
            warnings.append(f"when-line of '{action}' overlaps '{a['action']}' ({a['project']}): "
                            f"Jaccard {round(j, 2)} >= {C.POLICY_OVERLAP}")
    return errors, warnings


def check_rule(action: str, sub_action: str, content: str, project: str) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    if len(content or "") > C.POLICY_RULE_MAX:
        errors.append(f"rule '{action}/{sub_action}' is {len(content)} chars; the limit is {C.POLICY_RULE_MAX}")
    current = store.plan(action, project)
    before = sum(len(r.get("content") or "") for r in current)
    others = sum(len(r.get("content") or "") for r in current if r["sub_action"] != sub_action)
    total = others + len(content or "")
    if total > C.POLICY_PLAN_MAX and total > before:
        errors.append(f"plan '{action}' would be {total} chars; the limit is {C.POLICY_PLAN_MAX}")
    return errors, warnings


def hook_total(rules: list[dict]) -> int:
    return sum(len(render(r.get("content"))) + 2 for r in rules)


def check_hook(hook_type: str, name: str, content: str) -> tuple[list[str], list[str]]:
    from app import hooks

    limit = C.HOOK_MAX.get(hook_type)
    if not limit:
        return [], []
    rules = [r for r in hooks.list_rules(hook_type) if r["enabled"]]
    before = hook_total(rules)
    after = hook_total([r for r in rules if r["name"] != name] + [{"content": content}])
    if after > limit and after > before:
        return [f"hook {hook_type} would render {after} chars; the budget is {limit}"], []
    return [], []


def gate(errors: list[str], warnings: list[str], body: dict | None):
    """None when allowed (errors demoted to warnings with allow_oversize + reason), else a 422 JSONResponse."""
    body = body or {}
    if not errors:
        return None
    if body.get("allow_oversize") and str(body.get("reason") or "").strip():
        warnings.extend(f"allowed oversize: {e}" for e in errors)
        errors.clear()
        return None
    return JSONResponse(status_code=422, content={
        "error": "POLICY-LINT", "errors": errors, "warnings": warnings,
        "hint": "shorten the text; allow_oversize with a reason is for legacy rows only"})


def _older(ts: str | None, cutoff: datetime) -> bool:
    if not ts:
        return True
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return t < cutoff
    except Exception:  # noqa: BLE001
        return True


def report() -> dict:
    from app import hooks

    actions = store.all_actions()
    rules = store.all_rules()
    over_desc = [{"project": a["project"], "action": a["action"], "chars": len(a["description"] or "")}
                 for a in actions if len(a["description"] or "") > C.POLICY_DESC_MAX]
    over_rules = [{"project": r["project"], "action": r["action"], "sub_action": r["sub_action"],
                   "chars": len(r["content"] or "")} for r in rules if len(r["content"] or "") > C.POLICY_RULE_MAX]
    plan_sizes: dict[tuple, int] = {}
    for r in rules:
        k = (r["project"], r["action"])
        plan_sizes[k] = plan_sizes.get(k, 0) + len(r["content"] or "")
    over_plans = [{"project": p, "action": a, "chars": n} for (p, a), n in sorted(plan_sizes.items())
                  if n > C.POLICY_PLAN_MAX]
    hook_rep = []
    for ht in sorted({r["hook_type"] for r in hooks.list_rules(None)} | set(C.HOOK_MAX)):
        rs = [r for r in hooks.list_rules(ht) if r["enabled"]]
        chars = hook_total(rs)
        budget = C.HOOK_MAX.get(ht)
        hook_rep.append({"hook_type": ht, "rules": len(rs), "chars": chars, "budget": budget,
                         "over": bool(budget and chars > budget)})
    overlaps = []
    by_proj: dict[str, list[dict]] = {}
    for a in actions:
        by_proj.setdefault(a["project"], []).append(a)
    for p, lst in sorted(by_proj.items()):
        lst = sorted(lst, key=lambda x: x["action"])
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                s = jaccard(lst[i]["description"], lst[j]["description"])
                if s >= C.POLICY_OVERLAP:
                    overlaps.append({"project": p, "a": lst[i]["action"], "b": lst[j]["action"],
                                     "jaccard": round(s, 3)})
    cutoff = datetime.now(timezone.utc) - timedelta(days=C.POLICY_UNUSED_DAYS)
    unused = [{"project": a["project"], "action": a["action"]} for a in actions
              if _older(a["updated_at"], cutoff) and _older(store.last_fetch(a["action"]), cutoff)]
    ok = not (over_desc or over_rules or over_plans or any(h["over"] for h in hook_rep))
    return {
        "limits": {"description": C.POLICY_DESC_MAX, "rule": C.POLICY_RULE_MAX, "plan": C.POLICY_PLAN_MAX,
                   "overlap": C.POLICY_OVERLAP, "unused_days": C.POLICY_UNUSED_DAYS, "hooks": dict(C.HOOK_MAX)},
        "oversize_descriptions": over_desc, "oversize_rules": over_rules, "oversize_plans": over_plans,
        "hooks": hook_rep, "overlaps": overlaps, "unused": unused, "ok": ok,
    }
