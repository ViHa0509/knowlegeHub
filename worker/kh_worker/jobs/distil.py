"""Distil job (spec §4.6): per project loop `next -> model -> submit`.

Stops when KH returns no batch_id, `done`/`remaining_after == 0`, the same log set is served twice (`repeat`),
`max_batches`, a cap, or lease loss. Dry mode: one batch per project (the cursor never moves). Two attempts per
batch (model/schema failure retried with the problems appended); when KH rejects some items of a real submit
(batch open) one repair call asks for corrected versions of only those, resent on the same batch_id. A batch
failing twice is left unsubmitted and the project is abandoned for this run.
"""
from __future__ import annotations

from kh_worker.budget import BudgetExceeded
from kh_worker.jobs import Ctx, kh_data, load_prompt

KINDS = ["lesson", "fact", "procedure", "habit", "policy_proposal"]


def schema(catalog: dict) -> dict:
    actions = list(dict.fromkeys(["-", *(catalog.get("actions") or [])]))
    item = {
        "type": "object", "additionalProperties": False,
        "required": ["kind", "action", "context", "title", "text", "entities", "sources"],
        "properties": {
            "kind": {"type": "string", "enum": KINDS},
            "action": {"type": "string", "enum": actions},
            "context": {"type": "string"},
            "title": {"type": "string"},
            "text": {"type": "string"},
            "entities": {"type": "array", "items": {"type": "string"}},
            "sources": {"type": "array", "items": {"type": "string"}},
            "updates": {"type": "string"},
            "contradicts": {"type": "string"},
            "target_action": {"type": "string"},
            "target_sub_action": {"type": "string"},
            "share": {"type": "boolean"},
        },
    }
    return {
        "title": "distil", "type": "object", "additionalProperties": False,
        "required": ["items", "contexts", "signal_words"],
        "properties": {
            "items": {"type": "array", "items": item},
            "contexts": {"type": "array", "items": {"type": "object", "required": ["tag", "description"],
                                                    "properties": {"tag": {"type": "string"},
                                                                   "description": {"type": "string"}}}},
            "signal_words": {"type": "array", "items": {"type": "string"}},
        },
    }


def check_output(out: dict, batch: dict) -> list[str]:
    """Worker-side checks before KH. Returns a list of problems (empty = ok)."""
    problems = []
    if not isinstance(out, dict) or not isinstance(out.get("items"), list):
        return ["output must be an object with an items list"]
    cat = batch.get("catalog") or {}
    actions = set(cat.get("actions") or []) | {"-"}
    tags = {c.get("tag") for c in (cat.get("contexts") or [])} | {c.get("tag") for c in (out.get("contexts") or [])
                                                                    if isinstance(c, dict)}
    neigh = {ln.split(" ", 1)[0] for ln in (batch.get("neighbours") or [])}
    for i, it in enumerate(out["items"]):
        if not isinstance(it, dict):
            problems.append(f"item {i}: not an object")
            continue
        if not (1 <= len(it.get("title") or "") <= 120):
            problems.append(f"item {i}: title must be 1..120 characters")
        if not (1 <= len(it.get("text") or "") <= 600):
            problems.append(f"item {i}: text must be 1..600 characters")
        if it.get("action", "-") not in actions:
            problems.append(f"item {i}: action not in the catalog")
        ctx = it.get("context") or "-"
        if ctx != "-" and not ctx.startswith("ent__") and ctx not in tags:
            problems.append(f"item {i}: context not in the catalog (declare it under contexts)")
        if not it.get("sources"):
            problems.append(f"item {i}: sources missing")
        if it.get("kind") == "policy_proposal" and not (it.get("target_action") and it.get("target_sub_action")):
            problems.append(f"item {i}: policy_proposal needs target_action and target_sub_action")
        if it.get("updates") and it.get("contradicts"):
            problems.append(f"item {i}: updates and contradicts are exclusive")
        for rel in ("updates", "contradicts"):
            if it.get(rel) and it[rel] not in neigh:
                problems.append(f"item {i}: {rel} must be a neighbour id")
    return problems


def _user(batch: dict, problems: list[str] | None = None) -> str:
    data = {k: batch.get(k) for k in ("project", "logs", "neighbours", "people", "catalog", "rules")}
    s = kh_data("batch", data)
    if problems:
        s += "\n\nYour previous answer had these problems; fix them:\n- " + "\n- ".join(problems)
    return s


def _ask(ctx: Ctx, system: str, user: str, sch: dict):
    res = ctx.llm.complete(system, user, sch, ctx.max_tokens)
    return res.json, res.cost_usd


def run_project(ctx: Ctx, project: str) -> dict:
    system = load_prompt("distil")
    stats = {"batches": 0, "items": 0, "accepted": 0, "rejected": 0, "status": "ok"}
    seen_sets = set()
    for _ in range(max(1, ctx.max_batches)):
        ctx.check_lease()
        batch = ctx.kh.distill_next(project)
        if not batch.get("batch_id"):
            stats["status"] = "empty"
            break
        key = tuple(sorted(lg["id"] for lg in batch.get("logs") or []))
        if key in seen_sets:
            stats["status"] = "repeat"
            break
        seen_sets.add(key)
        sch = schema(batch.get("catalog") or {})
        out, cost, problems = None, 0.0, None
        for _attempt in range(2):
            try:
                out, c = _ask(ctx, system, _user(batch, problems), sch)
                cost += c
            except BudgetExceeded:
                raise
            except Exception as e:  # noqa: BLE001
                problems = [f"model call failed: {e}"]
                out = None
                continue
            problems = check_output(out, batch)
            if not problems:
                break
            out = None
        if out is None:
            stats["status"] = "abandoned"
            ctx.event("worker.run", {"job": "distil", "project": project, "batch_id": batch["batch_id"],
                                     "status": "abandoned", "problems": problems})
            break
        body = {"project": project, "batch_id": batch["batch_id"], "items": out.get("items") or [],
                "contexts": out.get("contexts") or [], "signal_words": out.get("signal_words") or [],
                "distilled_by": f"worker:{getattr(ctx.llm, 'model', '')}", "cost_usd": round(cost, 6)}
        if ctx.mode != "on":
            body["dry_run"] = True
        res = ctx.kh.distill_submit(body)
        stats["batches"] += 1
        stats["items"] += len(body["items"])
        # (verdict, item) pairs; a repair replaces only the rejected items' verdicts.
        pairs = list(zip(res.get("results") or [], body["items"]))
        rejected = [r for r, _ in pairs if r.get("result") == "rejected"]
        if rejected and res.get("batch_open") and not res.get("dry_run"):
            fix = [{"item": body["items"][r["n"]], "reason": r.get("reason")} for r in rejected]
            try:
                out2, c2 = _ask(ctx, system, _user(batch) + "\n\n" + kh_data("rejected", fix) +
                                "\n\nReturn corrected versions of only these items.", sch)
                items2 = (out2 or {}).get("items") or []
                body2 = dict(body, items=items2, contexts=[], signal_words=[], cost_usd=round(c2, 6))
                res2 = ctx.kh.distill_submit(body2)
                pairs = ([(r, it) for r, it in pairs if r.get("result") != "rejected"] +
                         list(zip(res2.get("results") or [], items2)))
                rejected = [r for r, _ in pairs if r.get("result") == "rejected"]
            except BudgetExceeded:
                raise
            except Exception as e:  # noqa: BLE001
                ctx.log.warning("repair failed: %s", e)
        accepted = [r for r, _ in pairs if r.get("result") == "accepted"]
        stats["accepted"] += len(accepted)
        stats["rejected"] += len(rejected)
        if res.get("dry_run"):
            ctx.event("worker.run", {"job": "distil", "project": project, "batch_id": batch["batch_id"],
                                     "items": body["items"], "verdicts": res.get("results"), "dry_run": True})
        if ctx.auto_approve and ctx.mode == "on" and not res.get("dry_run"):
            ok = [(r, it) for r, it in pairs if r.get("result") == "accepted" and r.get("id")
                  and it.get("kind") != "policy_proposal"]
            ids = [r["id"] for r, _ in ok]
            share = [r["id"] for r, it in ok if it.get("share")]
            if ids:
                ctx.kh.distill_approve(project, ids, share)
        if ctx.mode != "on" or res.get("dry_run"):
            break
        if batch.get("done") or batch.get("remaining_after") == 0:
            break
    return stats


def run(ctx: Ctx) -> dict:
    out = {}
    for p in ctx.projects:
        try:
            out[p] = run_project(ctx, p)
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ in ("KHBudget", "LeaseLost"):
                raise
            out[p] = {"status": "error", "error": str(e)[:300]}
    return {"projects": out}

