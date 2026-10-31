"""Promote job (spec §4.6): one `GET /promote/next`, one model call, check, submit, one repair of KH-refused
items; never approves."""
from __future__ import annotations

from kh_worker.budget import BudgetExceeded
from kh_worker.jobs import Ctx, kh_data, load_prompt


def schema(common: list[dict]) -> dict:
    actions = [a["action"] for a in common] or ["-"]
    item = {"type": "object", "additionalProperties": False,
            "required": ["title", "text", "target_action", "target_sub_action", "sources", "why"],
            "properties": {"title": {"type": "string"}, "text": {"type": "string"},
                           "target_action": {"type": "string", "enum": actions},
                           "target_sub_action": {"type": "string"},
                           "sources": {"type": "array", "items": {"type": "string"}}, "why": {"type": "string"}}}
    return {"title": "promote", "type": "object", "additionalProperties": False, "required": ["items"],
            "properties": {"items": {"type": "array", "items": item}}}


def check(items: list, data: dict) -> list[dict]:
    rows = {r["id"]: r for r in data.get("rows") or []}
    rules = data.get("rules") or {}
    ok = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        if not (1 <= len(it.get("title") or "") <= 120 and 1 <= len(it.get("text") or "") <= rules.get("text_max", 600)):
            continue
        srcs = it.get("sources") or []
        if not srcs or any(s not in rows for s in srcs):
            continue
        if len({rows[s]["owner"] for s in srcs}) < int(rules.get("min_people", 2)):
            continue
        ok.append(it)
    return ok


def run(ctx: Ctx) -> dict:
    data = ctx.kh.promote_next()
    if not data.get("rows"):
        return {"status": "empty", "rows": 0}
    system = load_prompt("promote")
    sch = schema(data.get("common") or [])
    user = "\n\n".join([kh_data("rows", data.get("rows")), kh_data("common", data.get("common")),
                        kh_data("kernel", data.get("kernel")), kh_data("proposals", data.get("proposals")),
                        kh_data("rules", data.get("rules"))])
    res = ctx.llm.complete(system, user, sch, ctx.max_tokens)
    items = check((res.json or {}).get("items") or [], data)
    if not items:
        return {"status": "none", "rows": len(data["rows"]), "submitted": 0}
    body = {"items": items, "distilled_by": f"worker:{getattr(ctx.llm, 'model', '')}"}
    if ctx.mode != "on":
        body["dry_run"] = True
    out = ctx.kh.promote_submit(body)
    rejected = [r for r in out.get("results") or [] if r.get("result") == "rejected"]
    if rejected and not out.get("dry_run"):
        try:
            fix = [{"item": items[r["n"]], "reason": r.get("reason")} for r in rejected]
            res2 = ctx.llm.complete(system, user + "\n\n" + kh_data("rejected", fix) +
                                    "\n\nReturn corrected versions of only these items.", sch, ctx.max_tokens)
            items2 = check((res2.json or {}).get("items") or [], data)
            if items2:
                out2 = ctx.kh.promote_submit(dict(body, items=items2))
                out = dict(out2, results=[r for r in out.get("results") or [] if r.get("result") != "rejected"] +
                           list(out2.get("results") or []))
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001
            ctx.log.warning("promote repair failed: %s", e)
    return {"status": "ok", "rows": len(data["rows"]), "submitted": len(items), "results": out.get("results"),
            "dry_run": out.get("dry_run")}
