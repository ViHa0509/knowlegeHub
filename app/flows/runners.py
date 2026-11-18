"""Runners for the flows this build can execute.

KH 7.1 runs every flow on a generic step engine (blocks kh.call, logic.decide with Polars expressions,
data.merge, text.format, end.return). This build runs the flows the agent and kh-helper depend on with
fixed Python runners that answer the same result shape; the other seeded flows are listed and described
but answer status 'failed' with a clear message (docs/deviations.md).
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from app import ctx_util as U

Runner = Callable[[dict, Any], dict]
HOOK_PACK_CHARS = 9800  # KH_HOOK_PACK_CHARS (app/thresholds.py)


def _trace(*steps: tuple[str, str, bool, str]) -> list[dict]:
    return [{"id": i, "name": n, "b": "py", "ok": ok, "ms": 0, "exit": ex} for i, n, ok, ex in steps]


def _bound_space() -> Optional[str]:
    try:
        from app import personalization

        return personalization._bound_project()
    except Exception:  # noqa: BLE001
        return None


def resolve_space(inp: dict, text: str) -> tuple[Optional[str], str]:
    """(space, how): input.space, else the session's binding, else the best literal match."""
    from app.spaces import api as S

    sp = (inp.get("space") or "").strip()
    if sp and sp != "@bound":
        return sp, "named"
    b = _bound_space()
    if b:
        return b, "bound"
    m = S.match(text)
    if m["best_space"]:
        return m["best_space"], "found by the spaces step (no session binding)"
    return None, "none"


# ---------------------------------------------------------------------------
# task.full-retrieve
# ---------------------------------------------------------------------------


def full_retrieve(inp: dict, model) -> dict:
    from app.spaces import mapper as M
    from app.turn import mask

    text, masked = mask(str(inp.get("text") or ""))
    actions = M.split_actions(inp.get("actions"))
    space, how = resolve_space(inp, text)
    if space and not U.may(space, "project.read"):
        space, how = None, "not readable"
    les = M.lessons(text, actions, space, model)
    parts = [M.INTRO, ""]
    if les["lines"]:
        parts += [M.LESSONS_HEAD] + les["lines"] + [""]
    if space:
        mp = M.build_map(space, text, model, M.MAP_MAX_CHARS)
        parts.append(mp["text"])
        status = mp["status"]
    else:
        parts.append(M.MISSING + "\ncoverage: searched 0 rows in 0 branches you can read; hits in 0 branches; "
                     "status missing\npath: ~space\n" + M.RULE)
        mp = {"status": "missing", "ids": [], "best": [], "count": 0}
        status = "missing"
    body = "\n".join(parts).strip()
    return {
        "status": "done", "outcome": "done", "message": "",
        "items": [{"step": "Format the Map", "text": body, "status": status}],
        "text": body,
        "output": {"text": body, "steps": {
            "mask": {"text": text, "masked": masked},
            "space": {"best_space": space, "how": how},
            "lessons": {"count": les["count"], "no_lesson": les["no_lesson"], "actions": les["actions"],
                        "space": space},
            "fmt": {"status": status, "ids": mp.get("ids", []), "best": mp.get("best", []),
                    "count": mp.get("count", 0), "space": space}}},
        "trace": _trace(("mask", "Mask Secrets", True, "ok"), ("space", "Find the Space", True, "ok"),
                        ("lessons", "Lessons of the Task and Actions", True, "ok"),
                        ("map", "The Space Map", True, "ok"), ("fmt", "Format the Map", True, "ok"),
                        ("ret", "Return the Map", True, "end")),
    }


# ---------------------------------------------------------------------------
# Hook flows
# ---------------------------------------------------------------------------


def on_session_start(inp: dict, model) -> dict:
    from app.kernel import render_blocks, render_part

    try:
        part = int(inp.get("part") or 1)
    except (TypeError, ValueError):
        part = 1
    blocks = render_blocks(_bound_space(), U.personal_tier(), True)
    text, n = render_part(blocks, part, HOOK_PACK_CHARS)
    return {"status": "done", "outcome": "done", "message": "", "text": text, "parts": n,
            "trace": _trace(("kernel", "Load Kernel", True, "ok"), ("ret", "Return", True, "end"))}


TASK_FLOW_LINE = ("TASK FLOW: call {tool} for this task FIRST (text = the task; actions = the task's hidden actions, "
                  "';' between them, each named with the system it touches; follow the LESSONS it starts with), before "
                  "any other KH read; then open a row of the map (or of the lists above) with kh_expand(id), a '+N ... "
                  "under <id>' line of it with kh_expand(<id>), and look up a name or path with kh_find(key)")


def _space_about_block(space: Optional[str]) -> str:
    lines = ["SPACE ABOUT AND OPEN FINDINGS:"]
    if space:
        from app.spaces import frame as F

        a = F.about_row(space)
        if a:
            lines.append(U.short(a.get("source_text") or "", 1200))
    return "\n".join(lines)


def on_prompt_submit(inp: dict, model) -> dict:
    from app import turn
    from app.flows import store

    text = str(inp.get("text") or inp.get("prompt") or "")
    ctx = turn.build(text, model)
    best = (ctx.get("project") or {}).get("best")
    task = store.nearest_task_flow(text, model) if text.strip() else None
    out = [ctx["text"].rstrip(), "", _space_about_block(best)]
    if task:
        tool = "kh_" + task["id"].replace(".", "_").replace("-", "_")
        out += ["", TASK_FLOW_LINE.format(tool=tool)]
    body = "\n".join(out)
    return {"status": "done", "outcome": "done", "message": "", "project": best, "text": body,
            "task_flow": task,
            "trace": _trace(("mask", "Mask Secrets", True, "ok"), ("turn", "Turn Context", True, "ok"),
                            ("task", "Nearest Task Flow", True, "ok"))}


def on_pre_tool_use(inp: dict, model) -> dict:
    from app.personalization import guards

    tool = str(inp.get("tool_name") or "")
    gs = guards(tool) if tool else []
    lines = []
    for g in gs:
        lines.append(f"[{(g.get('decision') or 'ask').upper()}] {g.get('text') or ''}".strip())
    return {"status": "done", "outcome": "done", "message": "", "text": "\n".join(lines), "guards": gs,
            "trace": _trace(("guards", "Guard the Tool", True, "ok"))}


def on_stop(inp: dict, model) -> dict:
    from app import hooks

    changed = str(inp.get("changed") or "").lower() in ("true", "1", "yes")
    text = hooks.served_text("Stop") if changed else ""
    return {"status": "done", "outcome": "done", "message": "", "text": text,
            "trace": _trace(("stop", "Stop Reminder", True, "ok"))}


def turn_context(inp: dict, model) -> dict:
    r = on_prompt_submit(inp, model)
    r.pop("task_flow", None)
    return r


def load_kernel(inp: dict, model) -> dict:
    from app.kernel import render_blocks

    proj = (inp.get("project") or "").strip() or _bound_space()
    text = "\n\n".join(render_blocks(proj, U.personal_tier(), True))
    return {"status": "done", "outcome": "done", "message": "", "text": text,
            "trace": _trace(("kernel", "Load Kernel", True, "ok"))}


def msg_inbox(inp: dict, model) -> dict:
    from app import messages

    got = messages.deliver((inp.get("project") or "").strip() or None, None)
    text = "\n".join(m.get("notice") or m.get("content") or "" for m in got.get("messages") or [])
    return {"status": "done", "outcome": "done", "message": "", "text": text, "output": got,
            "trace": _trace(("inbox", "Message Inbox", True, "ok"))}


# ---------------------------------------------------------------------------
# Save flows (one node per call: this build has no agent split step)
# ---------------------------------------------------------------------------

LESSON_KINDS = {"lesson", "feedback"}


def _save_space(inp: dict) -> Optional[str]:
    sp = (inp.get("space") or "").strip()
    return sp if sp and sp != "@bound" else _bound_space()


def _vector_add(text: str, project: Optional[str], scope: str, meta: dict) -> dict:
    import main

    body = main.VectorAddRequest(text=text, project=project, scope=scope, metadata=meta)
    return main.vector_add(body)


def save_knowledge(inp: dict, model) -> dict:
    from app.turn import mask

    text, _n = mask(str(inp.get("text") or "").strip())
    if not text:
        return {"status": "failed", "outcome": "failed", "message": "text is empty", "text": "", "trace": []}
    kind = (str(inp.get("kind") or "fact").strip().lower() or "fact")
    space = _save_space(inp) or U.personal_tier()
    scope = "lesson" if kind in LESSON_KINDS else "knowledge"
    meta = {"kind": kind}
    if inp.get("topic"):
        meta["topic"] = str(inp["topic"])[:200]
    got = _vector_add(text, space, scope, meta)
    rid = got.get("vector_id")
    shown = "lesson-learns" if scope == "lesson" else "knowledges"
    msg = f"saved 1 row: {space or 'orchestrator'}/{shown}/{rid} (kind {kind})"
    return {"status": "done", "outcome": "done", "message": "", "text": msg,
            "output": {"ids": [rid], "space": space, "scope": shown},
            "trace": _trace(("mask", "Mask Secrets", True, "ok"), ("save", "Save One Node", True, "ok"))}


def save_log(inp: dict, model) -> dict:
    from app.turn import mask

    if inp.get("row_id"):
        return {"status": "done", "outcome": "done", "message": "", "text": f"log {inp['row_id']} already stored",
                "output": {"ids": [inp["row_id"]]}, "trace": _trace(("written", "Written Already?", True, "ok"))}
    text, _n = mask(str(inp.get("text") or "").strip())
    if not text:
        return {"status": "failed", "outcome": "failed", "message": "text is empty", "text": "", "trace": []}
    fields = inp.get("fields") if isinstance(inp.get("fields"), dict) else {}
    meta = {k: v for k, v in fields.items() if k in ("topic", "subject", "ts", "timestamp")}
    space = _save_space(inp) or U.personal_tier()
    got = _vector_add(text, space, "log", meta)
    rid = got.get("vector_id")
    return {"status": "done", "outcome": "done", "message": "", "text": f"saved log {rid} in {space or 'orchestrator'}",
            "output": {"ids": [rid], "space": space},
            "trace": _trace(("mask", "Mask Secrets", True, "ok"), ("write", "Write the Log", True, "ok"))}


RUNNERS: dict[str, Runner] = {
    "save.knowledge": save_knowledge,
    "save.log": save_log,
    "task.full-retrieve": full_retrieve,
    "sys.on_session_start": on_session_start,
    "sys.on_prompt_submit": on_prompt_submit,
    "sys.on_pre_tool_use": on_pre_tool_use,
    "sys.on_stop": on_stop,
    "flow.turn-context": turn_context,
    "flow.load-kernel": load_kernel,
    "flow.kernel-merge": load_kernel,
    "msg.inbox": msg_inbox,
}

HOOK_FLOWS = {"SessionStart": "sys.on_session_start", "UserPromptSubmit": "sys.on_prompt_submit",
              "PreToolUse": "sys.on_pre_tool_use", "Stop": "sys.on_stop"}


def not_runnable(flow_id: str) -> dict:
    msg = (f"flow '{flow_id}' is described here but this build has no runner for it yet (the generic step "
           "engine of KH 7.1 is not built; see docs/upgrade-7.1-checklist.md item 3)")
    return {"status": "failed", "outcome": "failed", "message": msg, "text": "", "items": [], "trace": []}

