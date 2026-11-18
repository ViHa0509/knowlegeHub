"""The task map of a space (POST /spaces/{space}/map) and the LESSONS block of task.full-retrieve.

KH 7.1 builds the map from a key index, meaning search per scope, link groups and learned links. The
rebuild has no link index yet, so the map here is keys (exact match on row keys and names) + meaning
(per content scope, above MAP_FLOOR), rendered in the 7.1 text layout. See docs/deviations.md.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Optional

from app import config
from app import vector_store as vs
from app.ctx_util import short
from app.spaces import frame as F

MAP_FLOOR = config._float("KH_MAP_FLOOR", 0.55)
MAP_K = config._int("KH_MAP_K", 8)
MAP_MAX_CHARS = config._int("KH_MAP_MAX_CHARS", 4000)
LESSON_FLOOR = config._float("KH_MAP_LESSON_FLOOR", 0.5)
LESSON_PER_ACTION = config._int("KH_MAP_LESSONS_PER_ACTION", 2)
FACETS = ("what", "where", "solution", "history", "constraints", "who", "lessons", "impact")

INTRO = ("Open with kh_expand(id) the rows you use. The map shows only the rows closest to the task: when the task "
         "has several steps, checks or rules, or asks for everything known, ALSO open each '+N ... under <id>' line "
         "with kh_expand(<id>), kh_expand('~more') for the next rows by meaning, before you answer; stop when a page "
         "adds nothing new.")
MISSING = ("nothing in what you can see: no key and no row close in meaning; do not guess. The nearest rows (below "
           "the floor) and branches follow; check them before use.")
RULE = ("rule: open every MUST OPEN line; \"+N\" = cut, kh_expand(<id>) opens it; \"none found\" = nothing there")
LESSONS_HEAD = ("LESSONS (follow them: your own win over the space's, the space's over shared ones; open one with "
                "kh_expand(<its id as written>)):")

# '~more' pages: the rows a map cut, per caller (actor) and space, kept for an hour.
_MORE: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_MORE_LOCK = threading.Lock()
_MORE_TTL = 3600.0


def _caller() -> str:
    from app import auth

    try:
        return auth.current_actor() or "local"
    except Exception:  # noqa: BLE001
        return "local"


def remember_more(space: str, rows: list[dict]) -> None:
    with _MORE_LOCK:
        _MORE[(_caller(), space)] = (time.time(), rows)


def take_more(space: str, n: int = 8) -> list[dict]:
    with _MORE_LOCK:
        key = (_caller(), space)
        ts, rows = _MORE.get(key, (0.0, []))
        if time.time() - ts > _MORE_TTL:
            rows = []
        page, rest = rows[:n], rows[n:]
        _MORE[key] = (time.time(), rest)
        return page


def _encode(model, text: str):
    from app.embeddings import get_embedder

    m = model or get_embedder()
    return m.encode(text, normalize_embeddings=True)


def _visible(meta: dict, rid: str) -> bool:
    from app import auth

    try:
        return bool(auth.row_visible(meta, rid))
    except Exception:  # noqa: BLE001
        return True


def meaning(space: str, text: str, model=None, vec=None, scopes=F.CONTENT_SCOPES, k: int = MAP_K,
            where: Optional[dict] = None) -> tuple[list[dict], int, int]:
    """Rows close in meaning in the space: (rows best first, rows searched, branches with rows)."""
    vec = vec if vec is not None else _encode(model, text)
    hits: dict[str, dict] = {}
    searched = branches = 0
    for name in scopes:
        s = F.scope_of(name)
        if not s:
            continue
        had = False
        for h, lw in F.lanes(space, s):
            try:
                n = int(h.count())
            except Exception:  # noqa: BLE001
                n = 0
            if n <= 0:
                continue
            searched += n
            had = True
            try:
                got = vs.query_vt(h, vec, k, where=vs.and_where(lw, where))
            except Exception:  # noqa: BLE001
                continue
            for g in got:
                if g["metadata"].get("chunk_of") or not _visible(g["metadata"], g["vector_id"]):
                    continue
                row = F.row_out(space, s, g)
                old = hits.get(row["qid"])
                if old is None or row["score"] > old["score"]:
                    hits[row["qid"]] = row
        branches += 1 if had else 0
    rows = sorted(hits.values(), key=lambda r: -r["score"])
    return rows, searched, branches


def _terms(text: str) -> list[str]:
    words = re.findall(r"[\w.\-/]+", text or "")
    out = [w for w in words if len(w) >= 3]
    for n in (2, 3):
        out += [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]
    return out


def index_lookup(space: str, key: str, limit: int = 20) -> list[dict]:
    """Rows a key points to: exact (case-folded) match on id, name, subject or one of the row's keys."""
    want = (key or "").strip().casefold()
    if not want:
        return []
    out = []
    for name in F.CONTENT_SCOPES + ("about",):
        s = F.scope_of(name)
        for h, lw in F.lanes(space, s):
            try:
                rows = vs.meta_rows_vt(h, lw)
            except Exception:  # noqa: BLE001
                continue
            for rid, meta in rows:
                if meta.get("chunk_of"):
                    continue
                names = [rid, meta.get("name"), meta.get("subject"), meta.get("key"), meta.get("title")]
                names += [str(x) for x in F._jl(meta.get("keys"))]
                if any(isinstance(x, str) and x.strip().casefold() == want for x in names):
                    out.append({"qid": F.qualify(space, s["scope"], rid), "id": rid, "scope": s["scope"],
                                "name": F.row_name(meta, meta.get("source_text") or ""), "on": "key"})
                    if len(out) >= limit:
                        return out
    return out


def keys_in(space: str, text: str) -> list[dict]:
    seen, out = set(), []
    for t in _terms(text):
        for r in index_lookup(space, t, limit=5):
            if r["qid"] not in seen:
                seen.add(r["qid"])
                out.append(r)
    return out


def _line_best(r: dict) -> str:
    kind = r.get("kind") or r["type"]
    head = f"{r['id']} | {r['space']}/{r['scope']} {kind} | {short(r['summary'], 90)}"
    quote = short(r.get("text") or r["summary"], 400).replace("\n", " ")
    return f"{head}\n  \"{quote}\""


def _line_flat(r: dict) -> str:
    kind = r.get("kind") or r["type"]
    sc = f" {r['score']:.2f}" if "score" in r else ""
    return f"[{r['id']}{sc}] {short(r['name'], 80)} | {r['space']}/{r['scope']} {kind} | {short(r['summary'], 150)}"


def build_map(space: str, text: str, model=None, max_chars: int = MAP_MAX_CHARS) -> dict:
    """{space, task, status, text, ids, best, count} for POST /spaces/{space}/map."""
    vec = _encode(model, text)
    rows, searched, branches = meaning(space, text, vec=vec)
    keyed = keys_in(space, text)
    key_rows = []
    for k in keyed:
        r = F.find_row(space, k["id"], k["scope"])
        if r:
            r["score"] = 1.0
            key_rows.append(r)
    above = [r for r in rows if r["score"] >= MAP_FLOOR]
    seen = set()
    best = []
    for r in key_rows + above:
        if r["qid"] not in seen:
            seen.add(r["qid"])
            best.append(r)
    hit_branches = len({r["scope"] for r in best})
    status = "ok" if best else "missing"
    lines: list[str] = []
    if status == "missing":
        lines.append(MISSING)
    lines.append(f"coverage: searched {searched} rows in {branches} branches you can read; hits in {hit_branches} "
                 f"branches; status {status}")
    lines.append("path: ~space")
    shown_best = best[:4]
    if shown_best:
        lines.append("[best]")
        lines.extend(_line_best(r) for r in shown_best)
    for f in FACETS:
        if f == "lessons":
            ls = [r for r in best if r["scope"] == "lesson-learns" or (r.get("kind") or r["type"]) == "lesson"]
            if ls:
                lines.append("[lessons]")
                lines.extend(_line_flat(r) for r in ls[:3])
                continue
        lines.append(f"[{f}] none found in what you can read")
    rest = [r for r in (best[4:] + [r for r in rows if r["qid"] not in seen])]
    flat, cut = rest[:5], rest[5:]
    if flat:
        lines.append("[flat]")
        lines.extend(_line_flat(r) for r in flat)
    if cut:
        b = cut[0]
        sc = f"{b['score']:.2f}" if "score" in b else "-"
        lines.append(f"+{len(cut)} more by meaning under ~more (best {sc}: {b['id']}): kh_expand('~more') before a "
                     "step-by-step or whole-picture answer")
    remember_more(space, cut)
    lines.append(RULE)
    body = "\n".join(lines)
    if len(body) > max_chars:
        body = body[:max_chars - 3] + "..."
    return {"space": space, "task": text, "status": status, "text": body, "count": len(best),
            "ids": [r["qid"] for r in best], "best": [r["qid"] for r in shown_best]}


# ---------------------------------------------------------------------------
# LESSONS of a task and its hidden actions
# ---------------------------------------------------------------------------


def _lesson_sources(space: Optional[str]) -> list[tuple[str, str, str, Optional[dict]]]:
    """(space, scope, label, where) in precedence order: own personal, the space's, lesson-learned."""
    from app import ctx_util as U

    out: list[tuple[str, str, str, Optional[dict]]] = []
    own = U.personal_tier()
    if own:
        out.append((own, "lesson-learns", "personal-lesson", None))
    if space and space != config.LESSON_PROJECT and not vs.is_personal(space):
        out.append((space, "lesson-learns", "lesson", None))
    if U.may(config.LESSON_PROJECT, "project.read"):
        out.append((config.LESSON_PROJECT, "knowledges", "knowledge", None))
    return out


def lessons(text: str, actions: list[str], space: Optional[str], model=None) -> dict:
    """{lines, count, no_lesson, actions}: the LESSONS block that starts a task map."""
    queries = [(text, None)] + [(a, a) for a in actions if a.strip()]
    sources = _lesson_sources(space)
    seen: dict[str, str] = {}
    lines: list[str] = []
    no_lesson: list[str] = []
    for q, action in queries:
        vec = _encode(model, q)
        found: list[tuple[dict, str]] = []
        for sp, sc, label, where in sources:
            rows, _n, _b = meaning(sp, q, vec=vec, scopes=(sc,), k=LESSON_PER_ACTION + 2, where=where)
            for r in rows:
                if r["score"] >= LESSON_FLOOR:
                    found.append((r, label))
            if len(found) >= LESSON_PER_ACTION:
                break
        found = found[:LESSON_PER_ACTION]
        if not found and action:
            no_lesson.append(action)
            lines.append(f"- NO LESSON for '{action}'")
            continue
        for r, label in found:
            for_part = f", for '{action}'" if action else ""
            if r["qid"] in seen:
                if action:
                    lines.append(f"- {r['qid']} ({label}{for_part}): the same lesson as above")
                continue
            seen[r["qid"]] = label
            body = short((r.get("text") or r["summary"]).replace("\n", " - "), 400)
            lines.append(f"- {r['qid']} ({label}{for_part}): {body}")
    return {"lines": lines, "count": len(seen), "no_lesson": no_lesson,
            "actions": [{"action": a, "level": 1, "needs": []} for a in actions if a.strip()]}


def split_actions(actions: Any) -> list[str]:
    if isinstance(actions, list):
        return [str(a).strip() for a in actions if str(a).strip()]
    return [a.strip() for a in str(actions or "").split(";") if a.strip()]
