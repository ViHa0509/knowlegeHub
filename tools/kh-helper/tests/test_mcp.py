import asyncio
import json
import time

from conftest import ISSUER, make_session
from kh_helper import mcp_server, quiet


def _text(result):
    """Extract the text of a call_tool result across SDK shapes."""
    if isinstance(result, tuple):
        result = result[0]
    if hasattr(result, "content"):
        result = result.content
    if isinstance(result, list):
        return "".join(getattr(c, "text", "") for c in result)
    return str(result)


def build(server_fake, instructions="I"):
    return mcp_server.build(make_session(), instructions_text=instructions)


def test_tool_list(signed_in, server):
    srv = build(server)
    tools = asyncio.run(srv.list_tools())
    names = sorted(t.name for t in tools)
    assert names == sorted(mcp_server.TOOL_NAMES) and len(names) == 15
    assert srv.name == "knowledge-hub"


def test_kh_save_with_read_back(signed_in, server):
    server.kh("GET", "/session/project", {"actor": "a", "binding": {"project": "alpha"}, "ttl_hours": 72})
    server.kh("POST", "/vector/add", {"status": "ok", "vector_id": "vec_1", "dimension": 384})
    server.kh("POST", "/vector/scroll", {"matched": 1, "count": 1, "hits": [{"vector_id": "vec_1"}]})
    srv = build(server)
    res = json.loads(_text(asyncio.run(srv.call_tool("kh_save", {"text": "X is Y.", "kind": "decision"}))))
    assert res["status"] == 200 and res["body"]["read_back"] is True and res["body"]["project"] == "alpha"
    add = server.find("POST", "/vector/add")[0].json
    assert add["scope"] == "knowledge" and add["project"] == "alpha"
    assert add["metadata"]["scope"] == "knowledge" and add["metadata"]["kind"] == "decision"
    assert add["metadata"]["topic"] == "decision" and add["metadata"]["ts"].endswith("Z")
    assert server.find("POST", "/vector/scroll")[0].json == {"ids": ["vec_1"], "scope": "knowledge",
                                                             "project": "alpha"}


def test_kh_log_missing_row_is_500(signed_in, server):
    server.kh("GET", "/session/project", {"binding": None})
    server.kh("POST", "/vector/add", {"status": "ok", "vector_id": "vec_2"})
    server.kh("POST", "/vector/scroll", {"hits": []})
    res = json.loads(_text(asyncio.run(build(server).call_tool("kh_log", {"text": "Fixed the bug. Then more."}))))
    assert res["status"] == 500
    meta = server.find("POST", "/vector/add")[0].json["metadata"]
    assert meta["subject"] == meta["topic"] == "Fixed the bug." and "project" not in meta


def test_kh_call_envelopes(signed_in, server):
    server.kh("GET", "/auth/me", {"email": "ann@example.com"})
    srv = build(server)
    res = json.loads(_text(asyncio.run(srv.call_tool("kh_call", {"method": "GET", "path": "/auth/me"}))))
    assert res == {"status": 200, "body": {"email": "ann@example.com"}}
    server.down = True
    res = json.loads(_text(asyncio.run(srv.call_tool("kh_me", {}))))
    assert res == {"status": 503, "error": quiet.SLOW_TOOL}


def test_not_signed_in_envelope(server):
    res = json.loads(_text(asyncio.run(build(server).call_tool("kh_me", {}))))
    assert res["status"] == 401 and "kh login" in res["error"]


def test_kh_members_and_message(signed_in, server):
    server.on("PUT", ISSUER + "/kh/projects/alpha/members/bob@x.com", lambda r: (200, {"ok": True}))
    srv = build(server)
    res = json.loads(_text(asyncio.run(srv.call_tool("kh_members", {"project": "alpha", "action": "add",
                                                                    "email": "Bob@X.com", "level": "editable"}))))
    assert res["status"] == 200 and server.requests[-1].json == {"level": "editable"}
    res = json.loads(_text(asyncio.run(srv.call_tool("kh_members", {"project": "alpha", "action": "remove"}))))
    assert res["status"] == 400
    server.kh("POST", "/messages/direct", {"id": 1})
    asyncio.run(srv.call_tool("kh_message", {"content": "hi", "to": "bob@x.com"}))
    assert server.find("POST", "/messages/direct")[0].json == {"to": "bob@x.com", "content": "hi"}


def test_kh_context(signed_in, server):
    server.kh("GET", "/kernel/render", lambda r: (200, f"part{r.params['part']}"))
    server.kh("GET", "/personalization/render", "P")
    srv = build(server)
    out = _text(asyncio.run(srv.call_tool("kh_context", {"event": "SessionStart"})))
    assert out == "part1\n\npart2\n\npart3"
    out = _text(asyncio.run(srv.call_tool("kh_context", {"event": "Nope"})))
    assert out.startswith("unknown event; use one of")


def test_instructions_rules_or_late(signed_in, server, monkeypatch):
    # The startup budget is measured from kh_helper's import (process start of the MCP server). In a long
    # pytest session (e.g. after the server suite) that moment is far in the past, so restart the clock here.
    monkeypatch.setattr(mcp_server, "STARTED", time.monotonic())
    server.kh("GET", "/kernel/render", lambda r: (200, "KERNEL" if r.params["part"] == "1" else ""))
    text = mcp_server.instructions(make_session())
    assert "KH SESSION RULES:\nKERNEL" in text
    monkeypatch.setenv("KH_HELPER_STARTUP_SECONDS", "0")
    assert mcp_server.LATE_RULES in mcp_server.instructions(make_session())
