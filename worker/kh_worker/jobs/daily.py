"""Daily job (spec §4.6; KH side `app/caps/daily.py`, needs KH_CAP_DAILY=on).

All served project-days are packed into one `complete_many` (batch API) fitting the remaining run budget; the rest
is deferred; failed answers are retried once in a second batch. The model returns
`{done, decided, broken_or_open, who, source_ids}`; code composes
`Done: ...\\nDecided: ...\\nBroken or open: ...\\nWho: ...` capped at 1200 chars by shortening the longest part.
On `leak_refused`/`validation_failed` one synchronous repair with KH's reason; 409 `write_conflict` is skipped.
"""
from __future__ import annotations

from kh_worker.budget import BudgetExceeded, estimate, tokens_of
from kh_worker.jobs import Ctx, kh_data, load_prompt
from kh_worker.llm import LLMResult

CAP = 1200
PARTS = (("done", "Done"), ("decided", "Decided"), ("broken_or_open", "Broken or open"), ("who", "Who"))
SCHEMA = {"title": "daily", "type": "object", "additionalProperties": False,
          "required": ["done", "decided", "broken_or_open", "who", "source_ids"],
          "properties": {"done": {"type": "string"}, "decided": {"type": "string"},
                         "broken_or_open": {"type": "string"}, "who": {"type": "string"},
                         "source_ids": {"type": "array", "items": {"type": "string"}}}}


def compose(ans: dict) -> str:
    vals = {k: (str(ans.get(k) or "").strip() or "none") for k, _ in PARTS}

    def text():
        return "\n".join(f"{label}: {vals[k]}" for k, label in PARTS)

    while len(text()) > CAP:
        k = max(vals, key=lambda x: len(vals[x]))
        over = len(text()) - CAP
        vals[k] = vals[k][:max(4, len(vals[k]) - over - 3)].rstrip() + "..."
        if len(vals[k]) <= 4:
            break
    return text()[:CAP]


def run(ctx: Ctx) -> dict:
    system = load_prompt("daily")
    days = []
    for p in ctx.projects:
        try:
            nxt = ctx.kh.daily_next(p)
        except Exception as e:  # noqa: BLE001
            if type(e).__name__ == "KHBudget":
                raise
            ctx.log.info("daily next for %s failed: %s", p, e)
            continue
        for d in nxt.get("days") or []:
            days.append((p, d))
    if not days:
        return {"status": "empty", "days": 0}
    reqs, picked = [], []
    left = ctx.llm.budget.remaining() if hasattr(ctx.llm, "budget") else float("inf")
    model = getattr(ctx.llm, "model", "")
    for p, d in days:
        user = kh_data("day", {"project": p, **d})
        est = estimate(model, tokens_of(system + user), ctx.daily_max_tokens, batch=True)
        if est > left:
            break
        left -= est
        reqs.append({"system": system, "user": user, "schema": SCHEMA, "max_tokens": ctx.daily_max_tokens})
        picked.append((p, d))
    results = ctx.llm.complete_many(reqs) if reqs else []
    retry = [i for i, r in enumerate(results) if not isinstance(r, LLMResult)]
    if retry:
        try:
            again = ctx.llm.complete_many([reqs[i] for i in retry])
        except BudgetExceeded as e:  # keep the answers already paid for; the rest waits for the next night
            ctx.log.info("daily retry batch refused: %s", e)
            again = []
        for i, r in zip(retry, again):
            results[i] = r
    written = skipped = 0
    for (p, d), r in zip(picked, results):
        if not isinstance(r, LLMResult):
            skipped += 1
            continue
        body = {"project": p, "day": d.get("day"), "text": compose(r.json or {}),
                "source_ids": (r.json or {}).get("source_ids") or [], "model": r.model,
                "cost_usd": round(r.cost_usd, 6), "built_from_ms": d.get("built_from_ms"), "rev": d.get("rev")}
        if ctx.mode != "on":
            body["dry_run"] = True
        try:
            ctx.kh.daily_submit(body)
            written += 1
        except BudgetExceeded:
            raise
        except Exception as e:  # noqa: BLE001
            reason = getattr(e, "body", {}) or {}
            err = reason.get("error") if isinstance(reason, dict) else None
            if err in ("leak_refused", "validation_failed"):
                try:
                    r2 = ctx.llm.complete(system, reqs[picked.index((p, d))]["user"] + "\n\nKH refused: " +
                                          str(reason)[:500], SCHEMA, ctx.daily_max_tokens)
                    ctx.kh.daily_submit(dict(body, text=compose(r2.json or {}),
                                             source_ids=(r2.json or {}).get("source_ids") or body["source_ids"],
                                             cost_usd=round(r.cost_usd + r2.cost_usd, 6)))
                    written += 1
                except Exception:  # noqa: BLE001
                    skipped += 1
            else:
                skipped += 1
    return {"status": "ok", "days": len(days), "served": len(picked), "deferred": len(days) - len(picked),
            "written": written, "skipped": skipped}
