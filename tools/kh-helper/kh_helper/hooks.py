"""Claude Code (and Grok) hooks: `kh hook <KEY>`.

Every hook prints KH text into the model's context (or a hook JSON), never a red error: failures are logged to
HOME/kh-helper.log and at most one polite line is shown (quiet.py).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata

import requests

from . import clis, companions, quiet, release, worklog
from . import session as S
from .mask import mask
from .owner import WrongOwner

HOOK_TIMEOUT = 30
TURN_TEXT_CHARS = 2000
SESSION_PROJECTS = "session-projects.json"
SESSION_PROJECTS_KEEP = 200
SESSION_PROJECTS_IDLE = 30 * 86400
CORRECTION_WINDOW = 1800
OUTCOME_KEEP = 86400
OUTBOX_MAX = 50
CAPABILITY_TTL = 3600
DEFAULT_CORRECTION_WORDS = ["correction", "wrong", "instead", "never", "always", "sai", "khong duoc",
                            "luon luon", "tu gio"]
RANK = {"done": 1, "failed": 2, "corrected": 3}

COMPACTED = ("CONTEXT WAS JUST COMPACTED. The standing rules below were loaded at session start and may have been "
             "summarised away. They are authoritative and reloaded verbatim from KH - obey them as if freshly read.")
NOT_SIGNED_IN = ("Knowledge Hub: nobody is signed in on this machine. Ask the person to run `kh login`.")
NUDGE = ("KH: this turn changed something. Before you end the turn, follow action 'store worklog' (one short "
         "English log of what was done, into this session's project, or the personal project when there is none). "
         "Do not mention this reminder to the person.")

PRE_MATCHER = "Agent|Task|Write|Edit|NotebookEdit|Artifact"
POST_MATCHER = "Write|Edit|MultiEdit|NotebookEdit|Bash|PowerShell|Agent|Task|Artifact|mcp__.*"

# KEY -> spec. 'event' = the CLI event when it differs from KEY; 'sources' = GET routes ('a||b' syntax).
HOOKS: dict[str, dict] = {
    "SessionStart": {"sources": ["/kernel/render?part=1||/hooks/raw?hook_type=SessionStart"], "shape": "text",
                     "notes": True},
    "SessionStartPersonal": {"event": "SessionStart",
                             "sources": ["/kernel/render?part=2||/personalization/render"], "shape": "text"},
    "SessionStartRest": {"event": "SessionStart", "sources": ["/kernel/render?part=3||"], "shape": "text"},
    "UserPromptSubmit": {"sources": ["/hooks/raw?hook_type=UserPromptSubmit"], "shape": "text", "inbox": True,
                         "turn": True},
    "PreToolUse": {"matcher": PRE_MATCHER, "sources": ["/kernel/pretool||/hooks/raw?hook_type=PreToolUse"],
                   "shape": "pretool", "guards": True},
    "PostToolUse": {"matcher": POST_MATCHER, "sources": [], "shape": "nudge"},
    "Stop": {"sources": ["/hooks/raw?hook_type=Stop"], "shape": "stop"},
    "PostCompact": {"sources": ["/kernel/render?part=1||/hooks/raw?hook_type=SessionStart"],
                    "shape": "postcompact"},
    "PostCompactPersonal": {"event": "PostCompact", "sources": ["/kernel/render?part=2||"],
                            "shape": "postcompact"},
    "PostCompactRest": {"event": "PostCompact", "sources": ["/kernel/render?part=3||"], "shape": "postcompact"},
}
GROK_KEYS = ("PreToolUse", "Stop")
GUARD_SKIP = {"Write", "Edit", "NotebookEdit", "Agent", "Task"}
READ_ONLY_TOOLS = {"Read", "Glob", "Grep", "WebFetch", "WebSearch", "ToolSearch", "TodoWrite", "AskUserQuestion"}
READ_ONLY_KH = {"kh_me", "kh_status", "kh_catalog", "kh_help", "kh_context"}
READ_ONLY_POSTS = ("/context/step", "/vector/query", "/vector/scroll")
LOG_CMD = re.compile(r"/vector/add.*['\"]scope['\"]\s*:\s*['\"]log['\"]", re.S)
LEGACY_MARKERS = ("/hooks/raw?hook_type=", "/personalization/render", "/hooks/turn_mutated")
OUR_MARKER = "kh_helper.cli hook"
SERVE_RE = re.compile(r'\\?"serve_id\\?"\s*:\s*\\?"([A-Za-z0-9_.:-]{1,200})')


def hook_seconds() -> float:
    return _float_env("KH_HELPER_HOOK_SECONDS", 20.0)


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def event_of(key: str) -> str:
    return HOOKS[key].get("event") or key


def g(payload: dict, snake: str, default=None):
    """Read a hook input key; camelCase (Grok) accepted everywhere."""
    if snake in payload:
        return payload[snake]
    parts = snake.split("_")
    camel = parts[0] + "".join(p.title() for p in parts[1:])
    return payload.get(camel, default)


# ---------------------------------------------------------------- KH text sources

def _as_text(body) -> str:
    if body is None:
        return ""
    if isinstance(body, (dict, list)):
        return json.dumps(body, ensure_ascii=False)
    return str(body)


def _text(session, route: str) -> str:
    """'a||b': first route with status < 300 wins (even if empty); an empty alternative = print nothing."""
    for alt in route.split("||"):
        alt = alt.strip()
        if not alt:
            return ""
        st, body = session.call("GET", alt)
        if st < 300:
            return _as_text(body).strip()
    return ""


def sources_text(session, key: str) -> str:
    return "\n\n".join(t for t in (_text(session, r) for r in HOOKS[key].get("sources") or []) if t)


# ---------------------------------------------------------------- tool guards

def _str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def guard_applies(when, tool_input: dict) -> bool:
    if not when:
        return True
    if not isinstance(when, dict):
        return False
    for field, allowed in when.items():
        values = allowed if isinstance(allowed, list) else [allowed]
        if _str((tool_input or {}).get(field)) not in [_str(v) for v in values]:
            return False
    return True


def tool_guard(session, tool: str, tool_input: dict) -> tuple[str, str] | None:
    """(decision, reason) when a guard of this person matches; any failure -> None (never blocks)."""
    if not tool or tool in GUARD_SKIP:
        return None
    try:
        st, body = session.call("GET", "/personalization/guards", params={"tool": tool})
        if st >= 300 or not isinstance(body, dict):
            return None
        hits = [gd for gd in body.get("guards") or [] if isinstance(gd, dict)
                and guard_applies(gd.get("when"), tool_input)]
        if not hits:
            return None
        deny = any((gd.get("decision") or "ask") == "deny" for gd in hits)
        if deny:
            head = f"KH rule of this person blocks this {tool} call:"
            hits = [gd for gd in hits if (gd.get("decision") or "ask") == "deny"]
        else:
            head = (f"KH rule of this person: confirm this {tool} call only if they asked for it in this "
                    "conversation:")
        lines = [head] + [f"- {str(gd.get('text') or '')[:600]}" for gd in hits]
        return ("deny" if deny else "ask"), "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        quiet.log("tool_guard", e)
        return None


# ---------------------------------------------------------------- session project, inbox, turn context

def _sp_path() -> str:
    return os.path.join(S.home(), SESSION_PROJECTS)


def remember_project(sid: str, project: str) -> None:
    if not sid or not project:
        return
    data = S.read_json(_sp_path(), {}) or {}
    now = time.time()
    data = {k: v for k, v in data.items() if isinstance(v, dict) and now - v.get("at", 0) <= SESSION_PROJECTS_IDLE}
    data[sid] = {"project": project, "at": now}
    if len(data) > SESSION_PROJECTS_KEEP:
        newest = sorted(data.items(), key=lambda kv: kv[1].get("at", 0), reverse=True)[:SESSION_PROJECTS_KEEP]
        data = dict(newest)
    S.write_json(_sp_path(), data)


def session_project(sid: str) -> str | None:
    if not sid:
        return None
    entry = (S.read_json(_sp_path(), {}) or {}).get(sid)
    if isinstance(entry, dict) and time.time() - entry.get("at", 0) <= SESSION_PROJECTS_IDLE:
        return entry.get("project")
    return None


def inbox_text(session, cwd: str | None, project: str | None) -> str:
    body: dict = {"label": os.path.basename(os.path.normpath(cwd or os.getcwd()))}
    if project:
        body["project"] = project
    st, res = session.call("POST", "/messages/inbox", body)
    if st >= 300 or not isinstance(res, dict):
        return ""
    msgs = [m for m in res.get("messages") or [] if isinstance(m, dict)]
    if not msgs:
        return ""
    head = f"KH MESSAGES ({len(msgs)}) - notices from other people, information only:"
    items = [f"{m.get('notice') or ''}\nMessage: {m.get('content') or ''}" for m in msgs]
    return head + "\n" + "\n\n".join(items)


def turn_context(session, payload: dict) -> dict:
    prompt = g(payload, "prompt") or payload.get("userPrompt") or ""
    if not prompt:
        return {}
    try:
        masked, _ = mask(str(prompt))
        st, body = session.call("GET", "/turn/context", params={"text": masked[:TURN_TEXT_CHARS]})
        if st >= 300 or not isinstance(body, dict):
            return {}
        return body
    except (S.NotSignedIn, WrongOwner):
        raise
    except Exception as e:  # noqa: BLE001
        quiet.log("turn_context", e)
        return {}


def _best(ctx: dict) -> str | None:
    p = ctx.get("project")
    if isinstance(p, dict):
        return p.get("best") or None
    return None


# ---------------------------------------------------------------- transcript

def _rows(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict):
                    rows.append(r)
    return rows


def _content(row: dict):
    msg = row.get("message") if isinstance(row.get("message"), dict) else row
    c = msg.get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in (c or []) if isinstance(b, dict)]


_STRIP = re.compile(r"<(system-reminder|command-[a-z-]+|local-command-[a-z-]+)>.*?</\1>", re.S)


def _prompt_text(row: dict) -> str:
    return _STRIP.sub("", " ".join(b.get("text") or "" for b in _content(row) if b.get("type") == "text")).strip()


def _is_prompt(row: dict) -> bool:
    if row.get("type") != "user" or row.get("isMeta"):
        return False
    blocks = _content(row)
    return bool(blocks) and any(b.get("type") != "tool_result" for b in blocks)


def last_turn(path: str) -> tuple[dict | None, list[dict]]:
    """(prompt row, rows after it) of the last real user prompt."""
    rows = _rows(path)
    for i in range(len(rows) - 1, -1, -1):
        if _is_prompt(rows[i]):
            return rows[i], rows[i + 1:]
    return None, []


def kh_tool(name: str) -> str | None:
    """'mcp__knowledge-hub__kh_call' -> 'kh_call'."""
    if not name:
        return None
    base = name.split("__")[-1]
    if base.startswith("kh_") and (name == base or name.startswith("mcp__")):
        return base
    return None


def is_read_only(name: str, tool_input: dict) -> bool:
    if name in READ_ONLY_TOOLS:
        return True
    kt = kh_tool(name)
    if not kt:
        return False
    if kt in READ_ONLY_KH:
        return True
    ti = tool_input or {}
    if kt == "kh_call":
        method = str(ti.get("method") or "").upper()
        path = S._unmangle(str(ti.get("path") or "")).split("?")[0]
        return method == "GET" or (method == "POST" and path in READ_ONLY_POSTS)
    if kt == "kh_members":
        return (ti.get("action") or "people") in ("people", "list")
    return False


def is_log_write(name: str, tool_input: dict) -> bool:
    ti = tool_input or {}
    if kh_tool(name) == "kh_call":
        body = ti.get("body")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                body = {}
        body = body if isinstance(body, dict) else {}
        meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
        path = S._unmangle(str(ti.get("path") or "")).split("?")[0]
        return (str(ti.get("method") or "").upper() == "POST" and path == "/vector/add"
                and "log" in (body.get("scope"), meta.get("scope")))
    if name in ("Bash", "PowerShell"):
        return bool(LOG_CMD.search(str(ti.get("command") or "")))
    return False


def _tool_uses(rows: list[dict]):
    for r in rows:
        if r.get("type") != "assistant":
            continue
        for b in _content(r):
            if b.get("type") == "tool_use":
                yield b


def turn_was_read_only(path: str | None) -> bool:
    if not path:
        return False
    try:
        prompt, rows = last_turn(path)
    except OSError:
        return False
    if prompt is None:
        return False
    if len(_prompt_text(prompt)) >= _int_env("KH_HELPER_PROMPT_CHARS", 120):
        return False
    said = sum(len(b.get("text") or "") for r in rows if r.get("type") == "assistant"
               for b in _content(r) if b.get("type") == "text")
    if said >= _int_env("KH_HELPER_DISCUSSION_CHARS", 800):
        return False
    return all(is_read_only(b.get("name") or "", b.get("input") or {}) for b in _tool_uses(rows))


def turn_logged_work(path: str | None) -> bool:
    if not path:
        return False
    try:
        _, rows = last_turn(path)
    except OSError:
        return False
    return any(is_log_write(b.get("name") or "", b.get("input") or {}) for b in _tool_uses(rows))


# ---------------------------------------------------------------- nudge (PostToolUse)

def _nudge_path(sid: str) -> str:
    return os.path.join(S.home(), "nudge", f"{S.actor_id(sid) or 'x'}.json")


def nudge(key: str, payload: dict, cli: str) -> str:
    """'' or the NUDGE text. Claude Code only, and only when the auto worklog is not enabled."""
    if cli != clis.CLAUDE or worklog.enabled(cli):
        return ""
    sid = str(g(payload, "session_id") or "")
    if not sid:
        return ""
    path = _nudge_path(sid)
    if key == "UserPromptSubmit":
        S.write_json(path, {"done": False, "at": time.time()})
        return ""
    if key != "PostToolUse" or g(payload, "agent_id"):
        return ""
    state = S.read_json(path, {}) or {}
    if state.get("done"):
        return ""
    name = str(g(payload, "tool_name") or "")
    ti = g(payload, "tool_input") or {}
    if is_log_write(name, ti):
        S.write_json(path, {"done": True, "at": time.time()})
        return ""
    if is_read_only(name, ti):
        return ""
    S.write_json(path, {"done": True, "at": time.time()})
    return NUDGE


# ---------------------------------------------------------------- step outcomes

def _out_dir() -> str:
    return os.path.join(S.home(), "outcomes")


def outcomes_capability(session) -> dict:
    env = (os.environ.get("KH_HELPER_OUTCOMES") or "").strip().lower()
    cap_path = os.path.join(_out_dir(), "capability.json")
    cached = S.read_json(cap_path, None)
    if isinstance(cached, dict) and time.time() - cached.get("at", 0) < CAPABILITY_TTL:
        cap = cached
    else:
        cap = {"on": False, "at": time.time()}
        try:
            st, body = session.call("GET", "/v1/capabilities")
            if st < 300 and isinstance(body, dict):
                for c in body.get("capabilities") or []:
                    if isinstance(c, dict) and c.get("name") == "outcomes":
                        limits = c.get("limits") or {}
                        helper = limits.get("helper", True)
                        cap["on"] = str(helper).lower() not in ("off", "false", "0", "no")
                        words = limits.get("correction_words")
                        if isinstance(words, list) and words:
                            cap["words"] = [str(w) for w in words[:200]]
        except Exception as e:  # noqa: BLE001
            quiet.log("outcomes capability", e)
            return {"on": env == "on"}
        S.write_json(cap_path, cap)
    if env in ("on", "off"):
        cap = {**cap, "on": env == "on"}
    return cap


def _fold(text: str) -> str:
    t = (text or "").replace("đ", "d").replace("Đ", "D")
    t = unicodedata.normalize("NFKD", t)
    return "".join(ch for ch in t if not unicodedata.combining(ch)).casefold()


def has_correction(text: str, words: list[str]) -> bool:
    t = _fold(text)
    return any(re.search(r"(?<!\w)" + re.escape(_fold(w)) + r"(?!\w)", t) for w in words if w)


def _state_path(sid: str) -> str:
    return os.path.join(_out_dir(), f"{S.actor_id(sid) or 'x'}.json")


def _load_state(sid: str) -> dict:
    st = S.read_json(_state_path(sid), {}) or {}
    st.setdefault("turn", [])
    st.setdefault("prev", {})
    st.setdefault("labels", {})
    st.setdefault("outbox", [])
    return st


def _save_state(sid: str, st: dict) -> None:
    now = time.time()
    st["labels"] = {k: v for k, v in st["labels"].items() if isinstance(v, list) and now - v[1] <= OUTCOME_KEEP}
    st["outbox"] = [o for o in st["outbox"] if now - o.get("at", now) <= OUTCOME_KEEP][-OUTBOX_MAX:]
    S.write_json(_state_path(sid), st)


def _label(st: dict, ids: list[str], outcome: str, source: str, error: str | None = None) -> None:
    up = []
    for i in ids:
        cur = st["labels"].get(i)
        if cur and RANK.get(cur[0], 0) >= RANK[outcome]:
            continue
        st["labels"][i] = [outcome, time.time()]
        up.append(i)
    if up:
        entry = {"serve_ids": up, "outcome": outcome, "source": source, "at": time.time()}
        if error:
            entry["error"] = error.strip().splitlines()[0][:300] if error.strip() else ""
        st["outbox"].append(entry)


def _failure(resp) -> str | None:
    if isinstance(resp, dict):
        if resp.get("is_error") or resp.get("isError"):
            return _str(resp.get("content") or resp.get("error") or "error")
        for k in ("exit_code", "exitCode", "returncode", "code"):
            v = resp.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and v != 0:
                return _str(resp.get("stderr") or resp.get("error") or f"{k} {v}")
    return None


def _harvest(name: str, tool_input: dict, result) -> list[str]:
    kt = kh_tool(name)
    path = str((tool_input or {}).get("path") or "")
    if not ((kt == "kh_call" and "/context/step" in path) or kt == "kh_step"):
        return []
    return SERVE_RE.findall(_str(result))


def outcome_served(sid: str, serve_id: str) -> None:
    if not sid or not serve_id:
        return
    st = _load_state(sid)
    if serve_id not in st["turn"]:
        st["turn"].append(serve_id)
    _save_state(sid, st)


def outcome_start(session, key: str, payload: dict) -> threading.Thread | None:
    """Local labelling from code signals + background send of the outbox."""
    sid = str(g(payload, "session_id") or "")
    if not sid or g(payload, "agent_id"):
        return None
    event = event_of(key) if key in HOOKS else key
    if event not in ("UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop"):
        return None
    cap = outcomes_capability(session)
    if not cap.get("on"):
        return None
    st = _load_state(sid)
    now = time.time()
    if event == "UserPromptSubmit":
        if st["turn"]:
            st["prev"] = {"ids": st["turn"], "at": now}
            st["turn"] = []
        prev = st.get("prev") or {}
        prompt = str(g(payload, "prompt") or payload.get("userPrompt") or "")
        if prev.get("ids") and now - prev.get("at", 0) <= CORRECTION_WINDOW and \
                has_correction(prompt, cap.get("words") or DEFAULT_CORRECTION_WORDS):
            _label(st, prev["ids"], "corrected", "correction")
            st["prev"] = {}
    elif event in ("PostToolUse", "PostToolUseFailure"):
        name = str(g(payload, "tool_name") or "")
        ti = g(payload, "tool_input") or {}
        resp = g(payload, "tool_response")
        for i in _harvest(name, ti, resp):
            if i not in st["turn"]:
                st["turn"].append(i)
        err = _failure(resp) if event == "PostToolUse" else _str(g(payload, "error") or "failed")
        if err and st["turn"]:
            _label(st, st["turn"], "failed", "tool", err)
    elif event == "Stop":
        tp = g(payload, "transcript_path")
        if tp:
            try:
                _, rows = last_turn(tp)
                uses = {b.get("id"): b for b in _tool_uses(rows)}
                for r in rows:
                    for b in _content(r):
                        if b.get("type") != "tool_result":
                            continue
                        u = uses.get(b.get("tool_use_id")) or {}
                        for i in _harvest(u.get("name") or "", u.get("input") or {}, b.get("content")):
                            if i not in st["turn"]:
                                st["turn"].append(i)
                        if b.get("is_error") and st["turn"]:
                            _label(st, st["turn"], "failed", "tool", _str(b.get("content")))
            except OSError:
                pass
        todo = [i for i in st["turn"] if i not in st["labels"]]
        if todo:
            _label(st, todo, "done", "stop")
        st["prev"] = {"ids": st["turn"], "at": now} if st["turn"] else st.get("prev") or {}
        st["turn"] = []
    _save_state(sid, st)
    if not st["outbox"]:
        return None
    t = threading.Thread(target=_send_outbox, args=(session, sid), daemon=True)
    t.start()
    t.join(_float_env("KH_HELPER_OUTCOME_GRACE", 0.25))
    return t


def _send_outbox(session, sid: str) -> None:
    try:
        st = _load_state(sid)
        while st["outbox"]:
            entry = st["outbox"][0]
            body = {k: entry[k] for k in ("serve_ids", "outcome", "source", "error") if k in entry}
            code, _ = session.call("POST", "/step/outcome", body)
            if code == 404:
                S.write_json(os.path.join(_out_dir(), "capability.json"), {"on": False, "at": time.time()})
                st["outbox"] = []
                break
            if code < 300 or (code < 500 and code not in (401, 429)):
                st["outbox"].pop(0)
                _save_state(sid, st)
                continue
            break
        _save_state(sid, st)
    except Exception as e:  # noqa: BLE001
        quiet.log("outcome send", e)


# ---------------------------------------------------------------- SessionStart notes

def hook_exe() -> str:
    exe = sys.executable
    if os.name != "nt":
        return f'"{exe}"'
    if " " in exe:
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(1024)
            if ctypes.windll.kernel32.GetShortPathNameW(exe, buf, 1024):
                exe = buf.value or exe
        except Exception:  # noqa: BLE001
            pass
    exe = exe.replace("\\", "/")
    if " " not in exe:
        return exe
    return f'"{exe}"' if git_bash() else f'& "{exe}"'


def git_bash() -> str | None:
    env = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if env and os.path.exists(env):
        return env
    git = shutil.which("git")
    if git:
        root = os.path.dirname(os.path.dirname(git))
        for cand in (os.path.join(root, "bin", "bash.exe"), os.path.join(root, "usr", "bin", "bash.exe")):
            if os.path.exists(cand):
                return cand
    return None


def hook_command(key: str, cli: str = clis.CLAUDE) -> str:
    cmd = f"{hook_exe()} -m kh_helper.cli hook {key}"
    return cmd + " --cli grok" if cli == clis.GROK else cmd


def hook_check(run=None) -> dict:
    """Run '<exe> --version' in each shell Claude Code may use for hooks."""
    run = run or subprocess.run
    out: dict = {}
    cmd = f"{hook_exe()} --version"
    shells: list[tuple[str, list[str]]] = []
    if os.name == "nt":
        gb = git_bash()
        if gb:
            shells.append(("git-bash", [gb, "-c", cmd]))
        ps = shutil.which("pwsh") or shutil.which("powershell")
        if ps:
            shells.append(("powershell", [ps, "-NoProfile", "-Command", cmd]))
    else:
        shells.append(("sh", ["/bin/sh", "-c", cmd]))
    for name, argv in shells:
        try:
            r = run(argv, capture_output=True, text=True, timeout=60)
            out[name] = "ok" if r.returncode == 0 else f"failed: {(r.stderr or r.stdout or '').strip()[:200]}"
        except Exception as e:  # noqa: BLE001
            out[name] = f"failed: {e}"
    return out


def settings_path(profile: str | None = None) -> str:
    return os.path.join(profile or clis.profile(clis.CLAUDE), "settings.json")


def stale_events(settings: dict) -> list[str]:
    hooks = (settings or {}).get("hooks") or {}
    ours_anywhere = any(OUR_MARKER in (h.get("command") or "") for groups in hooks.values()
                        for grp in groups or [] for h in (grp.get("hooks") or []))
    if not ours_anywhere:
        return []
    stale: list[str] = []
    for key, spec in HOOKS.items():
        ev = event_of(key)
        want = hook_command(key)
        ok = False
        for grp in hooks.get(ev) or []:
            for h in grp.get("hooks") or []:
                if h.get("command") == want and grp.get("matcher") == spec.get("matcher"):
                    ok = True
        if not ok and ev not in stale:
            stale.append(ev)
    return stale


def hooks_note(cli: str = clis.CLAUDE) -> str:
    if cli != clis.CLAUDE:
        return ""
    data = S.read_json(settings_path(), None)
    if not isinstance(data, dict):
        return ""
    stale = stale_events(data)
    if not stale:
        return ""
    return (f"KH HOOKS OUT OF DATE: the {', '.join(stale)} hook of this Claude Code profile does not match this "
            f"kh-helper. Run \"{sys.executable}\" -m kh_helper.cli setup now (safe to repeat) and tell the person to "
            "restart this app so the new hooks load.")


def update_note(session) -> str:
    info = release.latest(session)
    if not info or not info.get("version"):
        return ""
    cur = release.installed_version()
    if not release.newer(info["version"], cur):
        return ""
    return (f"KH HELPER UPDATE: kh-helper {info['version']} is published (this machine has {cur}). Tell the person "
            f"in one line, then, unless they decline, run: \"{sys.executable}\" -m kh_helper.cli upgrade and ask them "
            "to restart this app so the new version loads.")


def _notes(session, cli: str) -> list[str]:
    out = []
    for fn in (lambda: companions.note(cli), lambda: hooks_note(cli), lambda: update_note(session)):
        try:
            t = fn()
            if t:
                out.append(t)
        except Exception as e:  # noqa: BLE001
            quiet.log("session notes", e)
    return out


# ---------------------------------------------------------------- output shapes

def shape(key: str, text: str, guard: tuple[str, str] | None = None) -> str:
    kind = HOOKS[key]["shape"]
    if kind == "text":
        return text
    if kind == "pretool":
        if not text and not guard:
            return ""
        hso: dict = {"hookEventName": "PreToolUse", "additionalContext": text}
        if guard:
            hso["permissionDecision"] = guard[0]
            hso["permissionDecisionReason"] = guard[1]
        return json.dumps({"hookSpecificOutput": hso}, ensure_ascii=False)
    if kind == "stop":
        return json.dumps({"decision": "block", "reason": text}, ensure_ascii=False) if text else ""
    if kind == "postcompact":
        if not text:
            return ""
        return json.dumps({"hookSpecificOutput": {"hookEventName": "PostCompact",
                                                  "additionalContext": COMPACTED + "\n\n" + text}},
                          ensure_ascii=False)
    if kind == "nudge":
        return nudge_json(event_of(key), text) if text else ""
    return text


def nudge_json(event: str, text: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}},
                      ensure_ascii=False)


def notice(text: str, cli: str) -> str:
    if cli != clis.CLAUDE or not quiet.notice_due():
        return ""
    return json.dumps({"systemMessage": text}, ensure_ascii=False)


# ---------------------------------------------------------------- run_hook

def parse_payload(raw: str | bytes | None) -> dict:
    if raw is None:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig", "replace")
    raw = raw.lstrip("﻿").strip()
    if not raw:
        return {}
    try:
        d = json.loads(raw)
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def run_hook(key: str, raw_input: str | bytes | None = None, cli: str = clis.CLAUDE, session=None) -> str:
    """Return what the hook prints on stdout ('' = nothing). Never raises."""
    if os.environ.get("KH_HELPER_SUMMARIZER"):
        return ""
    if key not in HOOKS and key != "PostToolUseFailure":
        return ""
    payload = parse_payload(raw_input)
    try:
        session = session or S.Session()
    except Exception as e:  # noqa: BLE001
        quiet.log(f"hook {key}", e)
        return ""
    sid = str(g(payload, "session_id") or "")
    try:
        if sid:
            session.actor = S.actor_id(sid)
            S.remember_actor(session.actor)
    except Exception as e:  # noqa: BLE001
        quiet.log("remember_actor", e)
    try:
        outcome_start(session, key, payload)
    except Exception as e:  # noqa: BLE001
        quiet.log("outcome_start", e)
    if key in ("UserPromptSubmit", "PostToolUse"):
        try:
            n = nudge(key, payload, cli)
        except Exception as e:  # noqa: BLE001
            quiet.log("nudge", e)
            n = ""
        if n:
            return nudge_json(event_of(key), n)
    if key in ("PostToolUse", "PostToolUseFailure"):
        return ""
    return _run_hook(key, payload, cli, session)


def _stop_gates(payload: dict, cli: str) -> bool:
    if g(payload, "stop_hook_active"):
        return True
    if cli == clis.GROK and (payload.get("reason") or "end_turn") != "end_turn":
        return True
    tp = g(payload, "transcript_path")
    if turn_was_read_only(tp):
        return True
    if turn_logged_work(tp):
        return True
    return False


def _fetch(key: str, payload: dict, cli: str, session) -> str:
    spec = HOOKS[key]
    sid = str(g(payload, "session_id") or "")
    if key == "Stop":
        if worklog.enabled(cli):
            try:
                if worklog.queue(session, payload, cli, session_project(sid)):
                    return ""
            except (S.NotSignedIn, WrongOwner):
                raise
            except Exception as e:  # noqa: BLE001
                quiet.log("worklog queue", e)
        return shape(key, sources_text(session, key))
    if key == "PreToolUse":
        guard = None
        if spec.get("guards"):
            guard = tool_guard(session, str(g(payload, "tool_name") or ""), g(payload, "tool_input") or {})
        return shape(key, sources_text(session, key), guard)
    if key == "UserPromptSubmit":
        rules = sources_text(session, key)
        ctx = turn_context(session, payload)
        if ctx.get("serve_id"):
            try:
                outcome_served(sid, str(ctx["serve_id"]))
            except Exception as e:  # noqa: BLE001
                quiet.log("outcome_served", e)
        best = _best(ctx)
        if best:
            remember_project(sid, best)
        project = best or session_project(sid)
        try:
            inbox = inbox_text(session, g(payload, "cwd"), project)
        except (S.NotSignedIn, WrongOwner):
            raise
        except Exception as e:  # noqa: BLE001
            quiet.log("inbox", e)
            inbox = ""
        parts = [inbox, rules, str(ctx.get("text") or "").strip()]
        return "\n\n".join(p for p in parts if p)
    text = sources_text(session, key)
    if spec.get("notes"):
        notes = _notes(session, cli)
        text = "\n\n".join(p for p in [text] + notes if p)
    return shape(key, text)


def _run_hook(key: str, payload: dict, cli: str, session) -> str:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    if key == "Stop" and _stop_gates(payload, cli):
        return ""
    box: dict = {}

    def work():
        try:
            box["out"] = _fetch(key, payload, cli, session)
        except BaseException as e:  # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(hook_seconds())
    if t.is_alive():
        quiet.log(f"hook {key}", note="timeout")
        return notice(quiet.SLOW_STOP if key == "Stop" else quiet.SLOW, cli)
    err = box.get("err")
    if err is None:
        return box.get("out") or ""
    if isinstance(err, S.NotSignedIn):
        quiet.log(f"hook {key}", err)
        return NOT_SIGNED_IN if key == "SessionStart" else ""
    quiet.log(f"hook {key}", err)
    if isinstance(err, requests.RequestException):
        return notice(quiet.SLOW_STOP if key == "Stop" else quiet.SLOW, cli)
    return ""


# ---------------------------------------------------------------- kh_context (hook-less CLIs)

def keys_for(event: str) -> list[str]:
    return [k for k in HOOKS if k == event or HOOKS[k].get("event") == event]


def rules_text(session, event: str, project: str = "") -> str:
    """All hook texts for an event (no 10k cap); the inbox first when the event has one."""
    keys = keys_for(event)
    parts: list[str] = []
    if any(HOOKS[k].get("inbox") for k in keys):
        try:
            inbox = inbox_text(session, os.getcwd(), project or None)
            if inbox:
                parts.append(inbox)
        except (S.NotSignedIn, WrongOwner):
            raise
        except Exception as e:  # noqa: BLE001
            quiet.log("kh_context inbox", e)
    for k in keys:
        t = sources_text(session, k)
        if t:
            parts.append(t)
    return "\n\n".join(parts)


# ---------------------------------------------------------------- setup (Claude Code settings.json + MCP)

def _is_ours(cmd: str) -> bool:
    return OUR_MARKER in (cmd or "")


def _is_legacy(cmd: str) -> bool:
    return any(m in (cmd or "") for m in LEGACY_MARKERS)


def edit_settings(data: dict, remove: bool = False) -> tuple[dict, list[str], int, int]:
    """Pure settings.json edit -> (data, hooks_added, hooks_removed, legacy_replaced)."""
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    removed = legacy = 0
    for ev in list(hooks.keys()):
        groups = []
        for grp in hooks.get(ev) or []:
            if not isinstance(grp, dict):
                groups.append(grp)
                continue
            keep = []
            for h in grp.get("hooks") or []:
                cmd = (h or {}).get("command") or ""
                if _is_ours(cmd):
                    removed += 1
                elif not remove and _is_legacy(cmd):
                    legacy += 1
                else:
                    keep.append(h)
            if keep:
                groups.append({**grp, "hooks": keep})
        if groups:
            hooks[ev] = groups
        else:
            del hooks[ev]
    added: list[str] = []
    if not remove:
        for key, spec in HOOKS.items():
            grp: dict = {}
            if spec.get("matcher"):
                grp["matcher"] = spec["matcher"]
            grp["hooks"] = [{"type": "command", "command": hook_command(key), "timeout": HOOK_TIMEOUT}]
            hooks.setdefault(event_of(key), []).append(grp)
            added.append(key)
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    return data, added, removed, legacy


def _claude_envs(profile: str) -> list[dict]:
    base = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_PID")}
    envs = [{**base, "CLAUDE_CONFIG_DIR": profile}]
    if clis.is_default_claude_profile(profile):
        envs.append({k: v for k, v in base.items() if k != "CLAUDE_CONFIG_DIR"})
    return envs


def register_mcp_claude(profile: str, name: str, command: list[str] | None, remove: bool = False,
                        which=None, run=None) -> dict:
    """claude mcp remove/add -s user <name> -- <command...>"""
    which = which or shutil.which
    run = run or subprocess.run
    exe = which("claude")
    add = ["mcp", "add", "-s", "user", name, "--"] + list(command or [])
    if not exe:
        manual = None if remove else "claude " + " ".join(_q(a) for a in add)
        return {"server": name, "registered": False, "manual": manual}
    results = []
    for env in _claude_envs(profile):
        run([exe, "mcp", "remove", "-s", "user", name], env=env, capture_output=True, text=True, timeout=120)
        if not remove:
            r = run([exe] + add, env=env, capture_output=True, text=True, timeout=120)
            results.append(r.returncode == 0)
    return {"server": name, "registered": (not remove) and all(results), "removed": remove}


def _q(a: str) -> str:
    return f'"{a}"' if (" " in a or not a) else a


def setup_claude(kh_url: str, remove: bool = False, settings: str | None = None, no_mcp: bool = False,
                 check_shells: bool = True) -> dict:
    profile = clis.profile(clis.CLAUDE)
    path = settings or settings_path(profile)
    data: dict = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        data = json.loads(text) if text.strip() else {}
    bak = clis.backup(path)
    data, added, removed, legacy = edit_settings(data if isinstance(data, dict) else {}, remove)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    out = {"settings": path, "backup": bak, "hooks_added": added, "hooks_removed": removed,
           "hook_shells": hook_check() if (check_shells and not remove) else {},
           "legacy_hooks_replaced": legacy, "mcp_server": None, "companions": {}}
    if not no_mcp:
        out["mcp_server"] = register_mcp_claude(profile, clis.SERVER_NAME, [sys.executable] + clis.mcp_args(kh_url),
                                                remove)
        comp = {}
        names = list(companions.CATALOG) if remove else list(companions.installed())
        found = companions.installed()
        for n in names:
            info = found.get(n, {})
            comp[n] = register_mcp_claude(profile, n, [info.get("command", "")] + info.get("args", []), remove)
        out["companions"] = comp
    return out
