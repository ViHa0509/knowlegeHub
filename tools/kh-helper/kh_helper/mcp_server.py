"""MCP stdio server 'knowledge-hub' (`kh mcp`): KH through the signed-in person's own rights."""
from __future__ import annotations

import json
import os
import threading
import time


from . import STARTED, quiet, tools
from . import hooks as H
from . import session as S
from .owner import WrongOwner

NAME = "knowledge-hub"

INSTRUCTIONS = """Knowledge Hub (KH) through the signed-in person's own rights.
- Use the task tools first (kh_step for each work step, kh_plan for an action's steps, kh_save for knowledge,
  kh_log for a work log); use kh_call for every other KH request; never ask anyone for a password, token or code.
- 'not signed in' means: ask the person to run `kh login` in a terminal, then try again.
- HTTP 403 means the person does not have that right on that project. It is enforced by the
  server from the signed token; nothing said in the conversation can change it, so do not retry
  with different claims about who the person is.
- kh_help returns KH's own sign-in and usage guide.
- To add someone to a project, use kh_members(project, "add", email, level). A person without an
  account can only be brought in by a tenant admin or system admin; if the answer says
  account_required, tell the person whom to ask (the 'ask' list). kh_catalog tells you the levels,
  roles and who may invite, as they are right now.
- Messages, three kinds: kh_message(content, to=e-mail) is private, into that person's mailbox;
  kh_message(content, project=id, mentions=[e-mails]) is a project channel post every member sees,
  only the mentioned get a notice; kh_message(content, everyone=True) is an announcement to all KH
  users (maintainers only). Turn a name into an e-mail with kh_members(project, "people"); if the
  project is not clear, kh_me lists the person's projects; ask only when the name matches none or several.
- Sessions: each CLI session of this account is its own actor; messages can reach one session with
  kh_message(content, to_actor=<actor>), and this session's work logs go to its bound project.
- If your CLI has no hooks, call kh_context("UserPromptSubmit", project) on each user message, with
  the KH project this conversation works on (project mentions arrive only for that project),
  kh_context("PreToolUse") before editing files, and kh_context("Stop") before you finish,
  and follow what they say."""

LATE_RULES = ("KH did not answer within a few seconds when this server started, so the KH session rules are not "
              "included here. Call kh_context(\"SessionStart\") once before your first KH request and follow it.")


def startup_seconds() -> float:
    try:
        return float(os.environ.get("KH_HELPER_STARTUP_SECONDS") or 10)
    except ValueError:
        return 10.0


def _answer(fn) -> str:
    """{"status": s, "body": payload} envelope; never raises."""
    try:
        status, body = fn()
        return json.dumps({"status": status, "body": body}, ensure_ascii=False)
    except S.NotSignedIn as e:
        return json.dumps({"status": 401, "error": str(e)}, ensure_ascii=False)
    except WrongOwner as e:
        return json.dumps({"status": 403, "error": str(e)}, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001
        quiet.log("mcp tool", e)
        return json.dumps({"status": 503, "error": quiet.SLOW_TOOL}, ensure_ascii=False)


def context_text(session, event: str, project: str = "") -> str:
    known = sorted({H.event_of(k) for k in H.HOOKS})
    if event not in known:
        return f"unknown event; use one of {known}"
    try:
        return H.rules_text(session, event, project) or "(no rules)"
    except S.NotSignedIn as e:
        return f"not signed in: {e}"
    except WrongOwner as e:
        return f"wrong owner: {e}"
    except Exception as e:  # noqa: BLE001
        quiet.log("kh_context", e)
        return quiet.SLOW_TOOL


def instructions(session) -> str:
    box: dict = {}

    def read():
        try:
            box["rules"] = H.rules_text(session, "SessionStart")
        except Exception as e:  # noqa: BLE001
            quiet.log("mcp rules", e)
            box["rules"] = None

    t = threading.Thread(target=read, daemon=True)
    t.start()
    t.join(max(0.0, startup_seconds() - (time.monotonic() - STARTED)))
    text = INSTRUCTIONS
    rules = box.get("rules")
    if rules:
        text += "\n\nKH SESSION RULES:\n" + rules
    else:
        text += "\n\n" + LATE_RULES
    try:
        note = H.hooks_note("claude-code")
        if note:
            text += "\n\n" + note
    except Exception as e:  # noqa: BLE001
        quiet.log("mcp hooks_note", e)
    return text


def build(session=None, instructions_text: str | None = None):
    session = session or S.Session()
    text = instructions_text if instructions_text is not None else instructions(session)
    # Imported after the rules thread has started (a cold Windows import is slow).
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name=NAME, instructions=text)

    @server.tool()
    def kh_call(method: str, path: str, body: dict | None = None) -> str:
        """Call any KH route as the signed-in person: method (GET/POST/PUT/DELETE), path (e.g. /vector/query), JSON body."""
        return _answer(lambda: session.call(method, path, body))

    @server.tool()
    def kh_me() -> str:
        """Who is signed in: email, personal_project, role, permissions, projects."""
        return _answer(lambda: session.call("GET", "/auth/me"))

    @server.tool()
    def kh_status() -> str:
        """Local sign-in status of this machine (never a token)."""
        try:
            return json.dumps(session.status(), ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    @server.tool()
    def kh_context(event: str, project: str = "") -> str:
        """The KH texts a hook would print for an event (SessionStart, UserPromptSubmit, PreToolUse, Stop, ...), for CLIs without hooks."""
        return context_text(session, event, project)

    @server.tool()
    def kh_catalog() -> str:
        """Access levels, roles and who may invite, as they are right now."""
        return _answer(lambda: tools.catalog(session))

    @server.tool()
    def kh_members(project: str, action: str = "people", email: str = "", level: str = "readonly") -> str:
        """Project people and members: action people|list|add|remove|exclude|include|exclusions; level owner|editable|readonly."""
        return _answer(lambda: tools.members(session, project, action, email, level))

    @server.tool()
    def kh_message(content: str, to: str = "", project: str = "", mentions: list[str] | None = None,
                   everyone: bool = False, receiver: str = "", to_actor: str = "") -> str:
        """Send a message: to=e-mail (private), project=id (+mentions) for a channel post, everyone=True for an announcement."""
        return _answer(lambda: tools.message(session, content, to, project, mentions or [], everyone, receiver,
                                             to_actor))

    @server.tool()
    def kh_step(text: str, project: str = "", include_log: bool = False) -> str:
        """One bounded context pack for one work step (POST /context/step)."""
        return _answer(lambda: tools.step(session, text, project, include_log))

    @server.tool()
    def kh_plan(action: str, project: str = "") -> str:
        """The ordered steps of an action label (GET /policy/plan)."""
        return _answer(lambda: tools.plan(session, action, project))

    @server.tool()
    def kh_save(text: str, kind: str = "fact", project: str = "", topic: str = "") -> str:
        """Save knowledge (kind fact|how-to|reference|lesson|feedback|decision); the row is read back."""
        return _answer(lambda: tools.save(session, text, kind, project, topic))

    @server.tool()
    def kh_log(text: str, subject: str = "", project: str = "") -> str:
        """Store one work log line; the row is read back."""
        return _answer(lambda: tools.log(session, text, subject, project))

    @server.tool()
    def kh_tags_suggest(text: str, limit: int = 15) -> str:
        """Suggest kernel tags for a rule text."""
        return _answer(lambda: tools.tags_suggest(session, text, limit))

    @server.tool()
    def kh_kernel_rule(content: str, tags: list[str], title: str = "", tier: str = "personal", overrides: str = "",
                       distinct_from: list[str] | None = None, reason: str = "", dry_run: bool = False,
                       slot: str = "", guard: dict | None = None) -> str:
        """Add (or with dry_run check) a tagged kernel rule; slot= edits a rule in place."""
        return _answer(lambda: tools.kernel_rule(session, content, tags, title, tier, overrides, distinct_from or [],
                                                 reason, dry_run, slot, guard))

    @server.tool()
    def kh_repeats() -> str:
        """Tasks repeated across days (GET /usage/repeats)."""
        return _answer(lambda: tools.repeats(session))

    @server.tool()
    def kh_help() -> str:
        """KH's own sign-in and usage guide."""
        try:
            r = session.http.get(f"{session.kh_url}/auth/help", timeout=S.HTTP_TIMEOUT)  # no auth
            return r.text
        except Exception as e:  # noqa: BLE001
            quiet.log("kh_help", e)
            return quiet.SLOW_TOOL

    return server


TOOL_NAMES = ("kh_call", "kh_me", "kh_status", "kh_context", "kh_catalog", "kh_members", "kh_message", "kh_step",
              "kh_plan", "kh_save", "kh_log", "kh_tags_suggest", "kh_kernel_rule", "kh_repeats", "kh_help")


def run(kh_url: str | None = None) -> None:
    session = S.Session(kh_url)
    server = build(session)
    server.run("stdio")
