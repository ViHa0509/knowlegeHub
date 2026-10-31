"""Task tools behind the MCP server (kh_step, kh_plan, kh_save, kh_log, kh_tags_suggest, kh_kernel_rule,
kh_repeats) and the identity helpers (kh_members). Each returns (status, body)."""
from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timezone

KINDS = ("fact", "how-to", "reference", "lesson", "feedback", "decision")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_project(session, project: str = "") -> str | None:
    """Explicit project, else the session's binding (GET /session/project), else None (KH -> personal)."""
    if project:
        return project
    try:
        st, body = session.call("GET", "/session/project")
        if st < 300 and isinstance(body, dict):
            binding = body.get("binding") or {}
            if isinstance(binding, dict) and binding.get("project"):
                return binding["project"]
    except Exception:  # noqa: BLE001
        pass
    return None


def step(session, text: str, project: str = "", include_log: bool = False):
    body: dict = {"text": text, "include_log": bool(include_log)}
    p = resolve_project(session, project)
    if p:
        body["project"] = p
    return session.call("POST", "/context/step", body)


def plan(session, action: str, project: str = ""):
    params = {"action": action}
    p = resolve_project(session, project)
    if p:
        params["project"] = p
    return session.call("GET", "/policy/plan", params=params)


def _add(session, text: str, scope: str, meta: dict, project: str = ""):
    """POST /vector/add, then read the row back ('ok' only proves the embedding was computed)."""
    p = resolve_project(session, project)
    metadata = {**meta, "scope": scope, "ts": now_iso()}
    body: dict = {"text": text, "scope": scope, "metadata": metadata}
    if p:
        metadata["project"] = p
        body["project"] = p
    st, got = session.call("POST", "/vector/add", body)
    if st >= 300 or not isinstance(got, dict) or not got.get("vector_id"):
        return st, got
    rb: dict = {"ids": [got["vector_id"]], "scope": scope}
    if p:
        rb["project"] = p
    read_back = False
    try:
        rst, rbody = session.call("POST", "/vector/scroll", rb)
        if rst < 300 and isinstance(rbody, dict):
            read_back = any((h or {}).get("vector_id") == got["vector_id"] for h in rbody.get("hits") or [])
    except Exception:  # noqa: BLE001
        read_back = False
    result = {**got, "project": p, "scope": scope, "read_back": read_back}
    return (st if read_back else 500), result


def save(session, text: str, kind: str = "fact", project: str = "", topic: str = ""):
    if kind not in KINDS:
        return 400, {"error": f"kind must be one of {', '.join(KINDS)}"}
    return _add(session, text, "knowledge", {"kind": kind, "topic": topic or kind}, project)


def first_sentence(text: str) -> str:
    t = " ".join((text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", t)
    return m.group(1) if m else t


def log(session, text: str, subject: str = "", project: str = ""):
    subject = (subject or first_sentence(text))[:80]
    return _add(session, text, "log", {"topic": subject, "subject": subject}, project)


def tags_suggest(session, text: str, limit: int = 15):
    return session.call("POST", "/kernel/tags/suggest", {"text": text, "limit": limit})


def kernel_rule(session, content: str, tags: list, title: str = "", tier: str = "personal", overrides: str = "",
                distinct_from: list | None = None, reason: str = "", dry_run: bool = False, slot: str = "",
                guard: dict | None = None):
    body: dict = {"tier": tier, "title": title, "content": content, "tags": list(tags or [])}
    if slot:
        body["slot"] = slot
    if guard:
        body["guard"] = guard
    if overrides:
        body["overrides"] = overrides
    if distinct_from:
        body["distinct_from"] = list(distinct_from)
        body["reason"] = reason
    return session.call("POST", "/kernel/rules/check" if dry_run else "/kernel/rules/add", body)


def repeats(session):
    return session.call("GET", "/usage/repeats")


# ---------------------------------------------------------------- identity (Keycloak KH extension)

MEMBER_ACTIONS = ("people", "list", "add", "remove", "exclude", "include", "exclusions")


def members(session, project: str, action: str = "people", email: str = "", level: str = "readonly"):
    p = urllib.parse.quote(project or "", safe="")
    if action == "people":
        return session.call_identity("GET", f"projects/{p}/people")
    if action == "list":
        return session.call_identity("GET", f"projects/{p}/members")
    if action == "exclusions":
        return session.call_identity("GET", f"projects/{p}/exclusions")
    if action not in MEMBER_ACTIONS:
        return 400, {"error": f"action must be one of {', '.join(MEMBER_ACTIONS)}"}
    if not email:
        return 400, {"error": f"action '{action}' needs an email"}
    e = urllib.parse.quote(email.strip().lower(), safe="@")
    if action == "add":
        return session.call_identity("PUT", f"projects/{p}/members/{e}", {"level": level})
    if action == "remove":
        return session.call_identity("DELETE", f"projects/{p}/members/{e}")
    if action == "exclude":
        return session.call_identity("PUT", f"projects/{p}/exclusions/{e}")
    return session.call_identity("DELETE", f"projects/{p}/exclusions/{e}")


def catalog(session):
    return session.call_identity("GET", "catalog")


def message(session, content: str, to: str = "", project: str = "", mentions: list | None = None,
            everyone: bool = False, receiver: str = "", to_actor: str = ""):
    if everyone:
        return session.call("POST", "/messages/announce", {"content": content})
    if project:
        m = list(mentions or [])
        if receiver and receiver != "all":
            m.append(receiver)
        return session.call("POST", "/messages", {"project": project, "content": content, "mentions": m})
    if to or receiver or to_actor:
        body: dict = {"to": to or receiver, "content": content}
        if to_actor:
            body["to_actor"] = to_actor
        return session.call("POST", "/messages/direct", body)
    return 400, {"error": "give to (an e-mail), project, or everyone=True"}
