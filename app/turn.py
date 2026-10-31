"""`GET /turn/context`: the per-message KH TURN CONTEXT (spec §3.4). Route group `turn`.

Project matching goes through `projects.match_projects` (§2/§3.10) when present, else `orchestrator.match_projects`,
else a local literal matcher with the same algorithm. Session binding (`app.session_project`), findings
(`app.feedback.list_findings`) and distilled items (`app.distill`) are optional; a missing part stays empty.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from fastapi import FastAPI, Query, Request

from app import config
from app import ctx_config as C
from app import ctx_util as U
from app.ctx_util import fold, short

log = logging.getLogger(__name__)

HEADER = ("KH TURN CONTEXT - computed by the hook for this message. It already answers /orchestrator/project_match, "
          "/policy/actions, /findings/list, /entities/match and the lesson query; do NOT call those again this turn.")
NONE_LINE = ("none matched literally - map by meaning, or stay project-less for small talk; a log or finding of "
             "project-less work goes to your personal project")
UNBOUND_TAIL = ("; THIS SESSION HAS NO PROJECT YET: once the work clearly belongs to one, bind it with PUT "
                "/session/project {\"project\":\"<id>\"} (a keyword match alone is not enough)")
NO_LESSONS = "KH LESSONS: none above the floor for the raw message."
MATCH_LIMIT = 4
RANK = {"id": 0, "alias": 1, "keyword": 2}


# ---------------------------------------------------------------------------
# Secret masking (copy of the §5.12 rules; app.eventing.mask wins when present)
# ---------------------------------------------------------------------------

CENSORED = "--censored--"
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_TOKENS = re.compile(r"\b(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9_-]{16,}|"
                     r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}|"
                     r"xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,})")
_BEARER = re.compile(r"(\bBearer\s+)([A-Za-z0-9._~+/=-]{8,})", re.I)
_URLPASS = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)([^@\s/]+)(@)", re.I)
_SECRET_WORDS = (r"passwords?|passwd|pwd|pass|passphrase|passcode|mật khẩu|mk|secret|client secret|api keys?|"
                 r"access keys?|private key|(?:access |refresh |auth )?tokens?|otp|credentials?")
_KEYED = re.compile(r"(\b(?:" + _SECRET_WORDS + r")\s*(?::|=>|->|=|\blà\b|\bis\b|\bbằng\b)\s*)([^\s,;]+)", re.I)
_PLAIN = {"expired", "invalid", "wrong", "incorrect", "missing", "required", "empty", "correct", "valid", "reset",
          "changed", "set", "none", "null", "sai", "hết", "hạn", "đúng", "rỗng"}


def mask(text: str) -> tuple[str, int]:
    try:
        from app.eventing.mask import mask as ev_mask  # type: ignore

        return ev_mask(text)
    except Exception:  # noqa: BLE001
        pass
    n = 0

    def sub(rx, repl, s):
        nonlocal n
        out, k = rx.subn(repl, s)
        n += k
        return out

    t = text or ""
    t = sub(_PEM, CENSORED, t)
    t = sub(_TOKENS, CENSORED, t)
    t = sub(_BEARER, lambda m: m.group(1) + CENSORED, t)
    t = sub(_URLPASS, lambda m: m.group(1) + CENSORED + m.group(3), t)

    def keyed(m):
        nonlocal n
        v = m.group(2)
        if len(v) < 4 or v.lower().strip(".") in _PLAIN or v == CENSORED:
            return m.group(0)
        n += 1
        return m.group(1) + CENSORED

    t = _KEYED.sub(keyed, t)
    return t, n


# ---------------------------------------------------------------------------
# Project match (§3.10)
# ---------------------------------------------------------------------------


def _local_match(text: str) -> list[dict]:
    from app import projects

    hay = " " + fold(text) + " "
    out = []
    for pid, e in projects.list_projects().items():
        terms: list[tuple[str, str]] = []
        for term, on in [(pid, "id")] + [(a, "alias") for a in e.get("aliases") or []] + \
                [(k, "keyword") for k in e.get("keywords") or []]:
            f = fold(term)
            if f and all(f != t[0] for t in terms):
                terms.append((f, on))
        best = None
        for f, on in terms:
            if (" " + f + " ") in hay:
                if best is None or len(f) > len(best[0]) or (len(f) == len(best[0]) and RANK[on] < RANK[best[1]]):
                    best = (f, on)
        if best:
            out.append({"project": pid, "matched_term": best[0], "matched_on": best[1], "term_length": len(best[0]),
                        "role": e.get("role") or "project",
                        "weak": best[1] == "keyword" and len(best[0]) < C.PROJECT_MATCH_WEAK_KEYWORD_LEN,
                        "source": "manifest-fallback"})
    out = [h for h in out if U.may(h["project"], "project.read")]
    out.sort(key=lambda h: (-h["term_length"], RANK[h["matched_on"]], h["project"]))
    return out


def match_projects(text: str) -> list[dict]:
    for modname in ("app.projects", "app.orchestrator"):
        try:
            import importlib

            mod = importlib.import_module(modname)
        except Exception:  # noqa: BLE001
            continue
        fn = getattr(mod, "match_projects", None)
        if callable(fn):
            return list(fn(text) or [])
    return _local_match(text)


def project_part(text: str) -> dict:
    hits = [h for h in match_projects(text) if U.may(h.get("project"), "project.read")]
    weak_only = bool(hits) and bool(hits[0].get("weak"))
    tie = len(hits) > 1 and hits[1].get("term_length") == hits[0].get("term_length") and \
        hits[1].get("matched_on") == hits[0].get("matched_on")
    best = hits[0]["project"] if hits and not weak_only and not tie else None
    return {"best": best, "ambiguous": bool(hits) and not weak_only and tie, "weak_only": weak_only,
            "matches": [{"project": h["project"], "term": h.get("matched_term"), "on": h.get("matched_on")}
                        for h in hits[:MATCH_LIMIT]],
            "_hits": hits}


def session_part(proj: dict) -> None:
    """Session binding (only with a principal and an X-KH-Actor session id); mutates `proj`."""
    from app import auth

    p = U.principal()
    actor = auth.current_actor()
    if p is None or not actor:
        return
    try:
        from app import session_project as sp
    except Exception:  # noqa: BLE001
        return
    owner = U.personal_tier()
    bound = sp.get(owner, actor) if callable(getattr(sp, "get", None)) else None
    if not bound and callable(getattr(sp, "detect", None)):
        # §2 signature: detect(principal, actor, text) binds `detected` itself when one strong match exists.
        bound = sp.detect(p, actor, proj.get("_text") or "")
    if not bound:
        proj["session"] = None
        return
    if callable(getattr(sp, "touch", None)):
        sp.touch(owner, actor)
    prompt_best = proj["best"]
    try:
        from app.eventing.emit import emit

        emit("kh.session.turn", {"project": bound.get("project"), "source": bound.get("source")},
             project=bound.get("project"), email=getattr(p, "email", None), actor=actor)
    except Exception:  # noqa: BLE001
        pass
    proj["best"] = bound.get("project")
    proj["ambiguous"] = False
    proj["weak_only"] = False
    proj["session"] = {"actor": actor, "project": bound.get("project"), "source": bound.get("source"),
                       "bound_at": bound.get("bound_at"), "last_seen": bound.get("last_seen"),
                       "prompt_names": prompt_best if prompt_best and prompt_best != bound.get("project") else None}


# ---------------------------------------------------------------------------
# Parts
# ---------------------------------------------------------------------------


def findings_part(best: str) -> list[str]:
    try:
        from app import feedback
    except Exception:  # noqa: BLE001
        return []
    fn = getattr(feedback, "list_findings", None)
    if not callable(fn):
        return []
    rows = fn(best)
    if isinstance(rows, dict):
        rows = rows.get("findings") or rows.get("hits") or []
    out = []
    for r in list(rows or [])[:C.TURN_FINDING_LIMIT]:
        if isinstance(r, str):
            out.append(short(r, 240))
            continue
        m = r.get("metadata") or {}
        txt = (r.get("desc") or m.get("desc") or r.get("text") or r.get("source_text") or r.get("issue_id")
               or m.get("issue_id") or "")
        out.append(short(txt, 240))
    return out


def entities_part(text: str, best: Optional[str]) -> list[str]:
    from app import entities

    scope = [best] if best else U.readable_projects()
    return [entities.line_of(c) for c in entities.match(text, scope, C.TURN_ENTITY_LIMIT)]


def distilled_part(text: str, best: Optional[str], action, context, ents, model) -> list[str]:
    from app import step

    lanes = []
    for ln in (best, C.TURN_LESSON_PROJECT):
        if ln and ln not in lanes and U.may(ln, "project.read"):
            lanes.append(ln)
    results = {ln: step._key_read(ln, action, context, ents or [], 6, text, model) for ln in lanes}
    return [step._line_of(item, with_sources=False) for _ln, item, _k in step._exact_first(results, action, context)]


def near_part(text: str, best: Optional[str], model) -> list[str]:
    from app.retriever import query_quick_knowledge

    K = C.TURN_DISTILLED_K
    if K <= 0 or not best:
        return []
    hits = query_quick_knowledge(text, k=2 * K, model=model, scope="knowledge",
                                 where={"topic": {"$eq": "distilled"}}, project=best)
    out = []
    for h in hits:
        if h["score"] < C.TURN_DISTILLED_FLOOR:
            continue
        body = U.collapse(h.get("source_text"))[:C.TURN_DISTILLED_CHARS]
        out.append(f"{h['vector_id']} ({round(h['score'], 3)}): {body}")
        if len(out) >= K:
            break
    return out


def _lesson_rows(text: str, scope: str, project: str, model, with_project: bool) -> list[dict]:
    from app import auth, step
    from app.retriever import query_quick_knowledge

    hits = step._withheld(query_quick_knowledge(text, k=C.TURN_LESSON_K, model=model, scope=scope, project=project))
    out = []
    for h in hits:
        if h["score"] < C.TURN_LESSON_FLOOR or not auth.row_visible(h.get("metadata"), h["vector_id"]):
            continue
        row = {"id": h["vector_id"], "score": round(h["score"], 3)}
        if with_project:
            row["project"] = project
        row["title"] = short(h.get("source_text") or h.get("piece") or "", 200)
        out.append(row)
    return out


def lessons_part(text: str, model) -> list[dict]:
    if not U.may(C.TURN_LESSON_PROJECT, "project.read"):
        return []
    return _lesson_rows(text, "knowledge", C.TURN_LESSON_PROJECT, model, False)


def my_lessons_part(text: str, model) -> list[dict]:
    own = U.personal_tier()
    if not own:
        return []
    return _lesson_rows(text, config.PERSONAL_LESSON_SCOPE, own, model, True)


def repeats_part(text: str) -> list[str]:
    from app import usage

    return usage.turn_lines(text)


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def project_line(proj: dict) -> str:
    sess = proj.get("session")
    if sess:
        line = (f"{sess['project']} - BOUND TO THIS SESSION ({sess.get('source')} {sess.get('bound_at')}); this "
                f"session's worklogs go there. Keep it; only when the person asks to work on another project, PUT "
                f"/session/project {{\"project\":\"<id>\"}}")
        if sess.get("prompt_names"):
            line += f" (this message also names '{sess['prompt_names']}': switch only if they ask)"
        return line
    if proj.get("best"):
        m = next((x for x in proj["matches"] if x["project"] == proj["best"]), None) or {}
        line = f"{proj['best']} (matched '{m.get('term')}' on {m.get('on')})"
    elif proj.get("ambiguous"):
        line = "AMBIGUOUS between " + ", ".join(m["project"] for m in proj["matches"]) + " - ask which one"
    else:
        line = NONE_LINE
    if "session" in proj and proj["session"] is None:
        line += UNBOUND_TAIL
    return line


def render(ctx: dict) -> str:
    proj = ctx["project"]
    pid = proj.get("best")
    lines = [HEADER, "PROJECT: " + project_line(proj)]
    if pid:
        lines.append(f"ACTIONS for {pid} (pick a label or none; if one fits, GET /policy/plan?action=<label>"
                     f"&project={pid} and follow it):")
    else:
        lines.append("ACTIONS (pick a label or none; if one fits, GET /policy/plan?action=<label> and follow it):")
    for a in ctx["actions"]:
        lines.append(f"  - '{a['action']}': {a['when']}")
    if pid:
        if ctx["findings"]:
            lines.append(f"FINDINGS ({pid}):")
            lines.extend(f"  - {f}" for f in ctx["findings"])
        else:
            lines.append(f"FINDINGS ({pid}): none")
    if ctx["entities"]:
        lines.append("PEOPLE AND THINGS NAMED:")
        lines.extend(f"  - {e}" for e in ctx["entities"])
    if ctx["near"]:
        lines.append(f"PROJECT KNOWLEDGE near this message (distilled from {pid}'s logs; whole row: POST /vector/scroll "
                     f"{{\"ids\":[...],\"scope\":\"knowledge\",\"project\":\"{pid}\"}}):")
        lines.extend(f"  - {n}" for n in ctx["near"])
    if ctx["distilled"]:
        lines.append("DISTILLED ITEMS for the action/context given (settled knowledge; [contested] = not settled):")
        lines.extend(f"  - {d}" for d in ctx["distilled"])
    if ctx["my_lessons"]:
        own = ctx["my_lessons"][0].get("project") or U.personal_tier()
        lines.append("YOUR OWN LESSONS near this message (they win over KH LESSONS on overlap; whole row: POST "
                     f"/vector/scroll {{\"ids\":[...],\"scope\":\"lesson\",\"project\":\"{own}\"}}):")
        lines.extend(f"  - {x['id']} ({x['score']}): {x['title']}" for x in ctx["my_lessons"])
    if ctx["repeats"]:
        lines.append("REPEATED ACROSS DAYS (counted by learn-back from your work logs; if this message is one of them, "
                     "follow 'suggest a tool' once):")
        lines.extend(f"  - {r}" for r in ctx["repeats"])
    if ctx["lessons"]:
        lines.append(f"KH LESSONS near this message (read a whole row with POST /vector/scroll {{\"ids\":[...],"
                     f"\"scope\":\"knowledge\",\"project\":\"{C.TURN_LESSON_PROJECT}\"}}):")
        lines.extend(f"  - {x['id']} ({x['score']}): {x['title']}" for x in ctx["lessons"])
    else:
        lines.append(NO_LESSONS)
    return "\n".join(lines)


def _part(name: str, fn, default):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        log.warning("turn part %s failed: %s", name, e)
        return default


def build(text: str, model, action: Optional[str] = None, context: Optional[str] = None,
          entities: Optional[list[str]] = None) -> dict:
    text, _masked = mask(text or "")
    text = text[:C.TURN_TEXT_CHARS]
    proj = _part("project", lambda: project_part(text),
                 {"best": None, "ambiguous": False, "weak_only": False, "matches": [], "_hits": []})
    proj.pop("_hits", None)
    proj["_text"] = text
    _part("session", lambda: session_part(proj), None)
    proj.pop("_text", None)
    best = proj.get("best")
    ctx: dict[str, Any] = {"project": proj}
    ctx["actions"] = _part("actions", lambda: _menu(best), [])
    ctx["findings"] = _part("findings", lambda: findings_part(best), []) if best else []
    ctx["entities"] = _part("entities", lambda: entities_part(text, best), [])
    ctx["distilled"] = (_part("distilled", lambda: distilled_part(text, best, action, context, entities, model), [])
                        if (action or context) else [])
    ctx["near"] = _part("near", lambda: near_part(text, best, model), [])
    ctx["lessons"] = _part("lessons", lambda: lessons_part(text, model), [])
    ctx["my_lessons"] = _part("my_lessons", lambda: my_lessons_part(text, model), [])
    ctx["repeats"] = _part("repeats", lambda: repeats_part(text), [])
    ctx["text"] = render(ctx)
    _part("prompt_event", lambda: prompt_event(text, best), None)
    return ctx


def prompt_event(masked_text: str, project: Optional[str]) -> None:
    from app import auth

    actor = auth.current_actor()
    if not actor or masked_text.lstrip().startswith("<task-notification>"):
        return
    from app.eventing.emit import emit

    emit("kh.session.prompt", {"text": masked_text, "project": project}, project=project, email=U.email(),
         actor=actor)


def attach(app: FastAPI) -> None:
    @app.get("/turn/context", tags=["context"])
    async def turn_context(request: Request, text: str, action: Optional[str] = None, context: Optional[str] = None,
                           entities: Optional[list[str]] = Query(None)):
        ents = entities or request.query_params.getlist("entities[]") or None
        return await asyncio.to_thread(build, text, U.model_of(request), action, context, ents)


def _menu(best: Optional[str]) -> list[dict]:
    from app.policy_actions import menu_for_turn

    return menu_for_turn(best)
