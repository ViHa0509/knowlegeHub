"""Auto work-log pipeline (1.15.0 / 1.18.2).

Replaces the Stop "block" (shown by Claude Code as a red "Stop hook error") with a silent, helper-written log.
The Stop hook queues a job file and spawns a hidden detached worker (`kh worklog`); the worker summarizes the
turn with the CLI's own summarizer (Claude Code: `claude -p --model haiku`, the person's own login) and stores
one `scope=log` row. Ctrl+C interrupts: Stop does not fire, so no log by design.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone

from . import quiet
from . import session as S

JOB_MARGIN = 180 + 60
JOB_KEEP = 3 * 86400
CLAIM_STALE = 900
SETTLE_SECONDS = 2
SUMMARIZE_TIMEOUT = 180
SLICE_CHARS = 12000
NO_LOG_PROJECTS = {"orchestrator", "lesson-learned", "market"}
SOURCE = "kh-helper auto worklog"

SYSTEM = ("You write one work log entry for the Knowledge Hub from a slice of a coding-assistant conversation. "
          "This turn has no tools. Answer with one JSON object only, no prose, no code fence.")

RULES_FALLBACK = ("Store a log whenever the turn changed something, discussed or decided a solution, or the person "
                  "gave new information worth keeping (a fact, a preference, a decision, a result). "
                  "Store nothing for small talk or a plain lookup.")

INSTRUCTIONS = (
    "Decide from the RULE whether this turn deserves a work log. Write in English even if the conversation is "
    "in another language; keep names, ids, paths, code and error texts verbatim. The log is 1-5 sentences: what "
    "was asked, what was done or decided, and the result. Never copy a secret (password, token, key) into it.\n"
    'Answer {"skip": true} when no log is needed, otherwise '
    '{"skip": false, "subject": "<2-6 word lowercase subject>", "text": "<the log>"}.'
)

_SECRET = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"
    r"|eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"
    r"|\bsk-[A-Za-z0-9_-]{8,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{16,}"
    r"|\bAKIA[0-9A-Z]{12,}"
    r"|((?:password|passwd|pwd|secret|token|api_key|client_secret)[\"']?\s*[=:]\s*[\"']?)[^\s\"',;]+"
)

# Test seams: the summarizer runner (subprocess.run-like) and the worker spawner.
RUNNER = subprocess.run
SPAWNER = None  # callable(job_path, handoff: dict) -> None; None = spawn()


class JobError(Exception):
    pass


def jobs_dir() -> str:
    return os.path.join(S.home(), "worklog")


def _off() -> bool:
    return os.environ.get("KH_HELPER_WORKLOG", "").strip().lower() in ("off", "0", "false", "no")


def builtin_command(cli: str) -> list[str] | None:
    if cli != "claude-code":
        return None
    exe = shutil.which("claude")
    if not exe:
        return None
    return [exe, "-p", "--model", "haiku", "--setting-sources", "", "--strict-mcp-config", "--tools", "",
            "--disable-slash-commands", "--no-session-persistence", "--output-format", "json",
            "--system-prompt", SYSTEM]


def command(cli: str = "claude-code") -> list[str] | None:
    raw = os.environ.get("KH_HELPER_WORKLOG_CMD")
    if raw:
        try:
            cmd = json.loads(raw)
            if isinstance(cmd, list) and cmd and all(isinstance(c, str) for c in cmd):
                return cmd
        except ValueError:
            pass
        return None
    return builtin_command(cli)


def enabled(cli: str = "claude-code") -> bool:
    return not _off() and command(cli) is not None


# ---------------------------------------------------------------- queue (inside the Stop hook)

def queue(session, payload: dict, cli: str = "claude-code", project: str | None = None) -> bool:
    if payload.get("agent_id") or payload.get("agentId"):
        return False
    sid = str(payload.get("session_id") or payload.get("sessionId") or "x")
    transcript = payload.get("transcript_path") or payload.get("transcriptPath")
    if not transcript:
        return False
    token = session.access_token()
    acting = session.acting_token if session.acting_token is not None else S.acting_from_env()
    os.makedirs(jobs_dir(), exist_ok=True)
    stem = f"{S.actor_id(sid) or 'x'}-{int(time.time() * 1000)}"
    path = os.path.join(jobs_dir(), stem + ".job")
    job = {"transcript": transcript, "session": sid, "cli": cli, "project": project,
           "at": S.now_iso(), "cwd": payload.get("cwd") or os.getcwd(), "acting": bool(acting)}
    S.write_json(path, job)
    handoff = {"access": token, "until": session.access_until, "acting": {stem: acting} if acting else {}}
    (SPAWNER or spawn)(path, handoff)
    return True


def _pythonw() -> str:
    exe = sys.executable
    if os.name == "nt":
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return exe


def spawn(job_path: str, handoff: dict) -> None:
    """Detached hidden worker; the token goes on stdin, never on disk."""
    env = {**os.environ, "KH_HELPER_SUMMARIZER": "1"}
    kw: dict = {"cwd": os.path.dirname(job_path), "env": env, "stdin": subprocess.PIPE,
                "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000 | 0x00000008 | 0x00000200  # NO_WINDOW|DETACHED|NEW_GROUP
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen([_pythonw(), "-m", "kh_helper.cli", "worklog"], **kw)
    try:
        p.stdin.write(json.dumps(handoff).encode("utf-8"))
        p.stdin.close()
    except OSError:
        pass


# ---------------------------------------------------------------- worker

def drain(handoff: dict, sleep=time.sleep) -> int:
    """Process every queued job; returns the number of logs stored."""
    sleep(SETTLE_SECONDS)
    sess = S.Session()
    until = float(handoff.get("until") or 0)
    sess.set_access(handoff.get("access") or "", until)
    acting_map = handoff.get("acting") or {}
    ctx: dict = {"rules": {}, "me": None}
    stored = 0
    d = jobs_dir()
    for path in sorted(glob.glob(os.path.join(d, "*.job"))):
        if time.time() >= until:
            break
        claimed = path[:-4] + ".claimed"
        try:
            os.replace(path, claimed)
        except OSError:
            continue  # another worker took it
        may_summarize = until - time.time() >= JOB_MARGIN
        try:
            r = _run_job(sess, claimed, acting_map, ctx, may_summarize)
        except S.NotSignedIn as e:
            quiet.log("worklog", e)
            _remove(claimed)
            continue
        except Exception as e:  # noqa: BLE001
            from .owner import WrongOwner
            quiet.log("worklog", e)
            if isinstance(e, WrongOwner):
                _back(claimed)
                break
            job = S.read_json(claimed, {}) or {}
            if _age(job) > JOB_KEEP:
                _remove(claimed)
            else:
                _back(claimed)
            continue
        if r == "stored":
            stored += 1
        elif r == "later":
            _back(claimed)
    now = time.time()
    for c in glob.glob(os.path.join(d, "*.claimed")):
        try:
            if now - os.path.getmtime(c) > CLAIM_STALE:
                os.replace(c, c[:-8] + ".job")
        except OSError:
            pass
    return stored


def _remove(p: str) -> None:
    try:
        os.remove(p)
    except OSError:
        pass


def _back(p: str) -> None:
    try:
        os.replace(p, p[:-8] + ".job")
    except OSError:
        pass


def _age(job: dict) -> float:
    try:
        at = datetime.strptime(job.get("at", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return time.time() - at.timestamp()
    except ValueError:
        return 0.0


def _run_job(sess, claimed: str, acting_map: dict, ctx: dict, may_summarize: bool) -> str:
    job = S.read_json(claimed, None)
    if not isinstance(job, dict):
        _remove(claimed)
        return "dropped"
    stem = os.path.basename(claimed)[:-8]
    sess.acting_token = acting_map.get(stem) or ""
    sid = str(job.get("session") or "")
    sess.actor = "" if sid == "x" else S.actor_id(sid)
    if job.get("acting") and not acting_map.get(stem):
        _remove(claimed)
        return "dropped"
    entry = job.get("entry")
    if not entry:
        if not may_summarize:
            return "later"
        sl = turn_slice(job.get("transcript") or "")
        if not sl:
            _remove(claimed)
            return "skipped"
        project, personal = _projects(sess, ctx, job.get("project"))
        entry = summarize(_rules(sess, ctx, project or personal), sl, job.get("cli") or "claude-code")
        if not entry:
            _remove(claimed)
            return "skipped"
        entry["project"] = project
        entry["personal"] = personal
        job["entry"] = entry
        S.write_json(claimed, job)
    store(sess, job)
    _remove(claimed)
    return "stored"


# ---------------------------------------------------------------- transcript slice

def _mask(text: str) -> str:
    def rep(m: re.Match) -> str:
        if m.group(1):
            return m.group(1) + "[masked]"
        if m.group(2):
            return m.group(2) + "[masked]"
        return "[masked]"
    return _SECRET.sub(rep, text)


def _rows(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                rows.append(r)
    return rows


def _content(row: dict):
    msg = row.get("message") if isinstance(row.get("message"), dict) else row
    return msg.get("content")


def _is_real_prompt(row: dict) -> bool:
    if row.get("type") != "user" or row.get("isMeta"):
        return False
    c = _content(row)
    if isinstance(c, str):
        return bool(c.strip())
    if isinstance(c, list):
        return any(isinstance(b, dict) and b.get("type") != "tool_result" for b in c)
    return False


def turn_slice(path: str) -> str:
    try:
        rows = _rows(path)
    except OSError:
        return ""
    start = None
    for i in range(len(rows) - 1, -1, -1):
        if _is_real_prompt(rows[i]):
            start = i
            break
    if start is None:
        return ""
    lines: list[str] = []
    for r in rows[start:]:
        c = _content(r)
        kind = r.get("type")
        blocks = [{"type": "text", "text": c}] if isinstance(c, str) else (c or [])
        for b in blocks:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" and (b.get("text") or "").strip():
                who = "PERSON" if kind == "user" else "ASSISTANT"
                lines.append(f"{who}: {b['text'].strip()}")
            elif t == "tool_use":
                lines.append(f"TOOL {b.get('name')}: {json.dumps(b.get('input'), ensure_ascii=False)[:400]}")
            elif t == "tool_result" and b.get("is_error"):
                res = b.get("content")
                if isinstance(res, list):
                    res = " ".join(x.get("text", "") for x in res if isinstance(x, dict))
                lines.append(f"TOOL FAILED: {str(res)[:300]}")
    text = _mask("\n".join(lines))
    if len(text) > SLICE_CHARS:
        head = SLICE_CHARS // 4
        tail = SLICE_CHARS - head - 5
        text = text[:head] + "\n[...]\n" + text[-tail:]
    return text


# ---------------------------------------------------------------- projects, rules, summarize, store

def _projects(sess, ctx: dict, session_project: str | None) -> tuple[str | None, str]:
    if ctx.get("me") is None:
        st, me = sess.call("GET", "/auth/me")
        if st >= 300 or not isinstance(me, dict) or not me.get("personal_project"):
            raise JobError(f"GET /auth/me gave no personal_project ({st})")
        ctx["me"] = me
    personal = ctx["me"]["personal_project"]
    p = session_project or None
    if p in NO_LOG_PROJECTS:
        p = None
    return p, personal


def _rules(sess, ctx: dict, project: str) -> str:
    cache = ctx.setdefault("rules", {})
    if project in cache:
        return cache[project]
    gate = RULES_FALLBACK
    try:
        st, body = sess.call("GET", "/policy/plan", params={"action": "store worklog", "project": project})
        if st < 300 and isinstance(body, dict):
            for rule in body.get("rules") or body.get("steps") or []:
                if isinstance(rule, dict) and rule.get("sub_action") == "gate" and rule.get("content"):
                    gate = rule["content"]
                    break
    except Exception as e:  # noqa: BLE001
        quiet.log("worklog rules", e)
    cache[project] = gate
    return gate


def _first_object(text: str) -> dict | None:
    start = text.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


def summarize(gate: str, slice_text: str, cli: str = "claude-code") -> dict | None:
    cmd = command(cli)
    if not cmd:
        raise JobError("no summarizer")
    prompt = f"RULE: {gate}\n\n{INSTRUCTIONS}\n\nTURN:\n{slice_text}"
    kw: dict = {"input": prompt.encode("utf-8"), "capture_output": True, "timeout": SUMMARIZE_TIMEOUT,
                "env": {**os.environ, "KH_HELPER_SUMMARIZER": "1"}}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000
    r = RUNNER(cmd, **kw)
    out = r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else (r.stdout or "")
    if r.returncode != 0:
        detail = ""
        try:
            detail = (json.loads(out) or {}).get("result") or ""
        except ValueError:
            detail = out[-300:]
        raise JobError(f"summarizer exit {r.returncode}: {detail}")
    text = out.strip()
    try:
        wrapped = json.loads(text)
        if isinstance(wrapped, dict) and "result" in wrapped and ("type" in wrapped or "is_error" in wrapped):
            if wrapped.get("is_error"):
                raise JobError(f"summarizer error: {wrapped.get('result')}")
            text = str(wrapped.get("result") or "")
    except ValueError:
        pass
    obj = _first_object(text)
    if not obj:
        return None
    if obj.get("skip") is True:
        return None
    subject = str(obj.get("subject") or "").strip().lower()
    body = str(obj.get("text") or "").strip()
    if not body:
        return None
    return {"subject": subject[:80] or "work log", "text": body}


def store(sess, job: dict) -> None:
    entry = job["entry"]
    day = (job.get("at") or S.now_iso())[:10]
    project = entry.get("project") or entry.get("personal")
    personal = entry.get("personal")
    subject = entry.get("subject") or "work log"

    def body_for(p: str, intended: str | None = None) -> dict:
        meta = {"scope": "log", "project": p, "subject": subject, "topic": subject, "ts": S.now_iso(),
                "source": SOURCE}
        if intended:
            meta["intended_project"] = intended
        return {"text": f"{day} {entry['text']}", "scope": "log", "metadata": meta}

    st, res = sess.call("POST", "/vector/add", body_for(project))
    if st == 403 and personal and project != personal:
        st, res = sess.call("POST", "/vector/add", body_for(personal, project))
    if st >= 500 or st in (401, 429):
        raise JobError(f"store failed {st}: {str(res)[:200]}")
    if st >= 300:
        quiet.log("worklog store", note=f"gave up {st}: {str(res)[:200]}")
