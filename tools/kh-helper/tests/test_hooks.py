import json
import threading

import pytest

from conftest import make_session
from kh_helper import hooks as H
from kh_helper import quiet


def run(key, payload=None, cli="claude-code"):
    return H.run_hook(key, json.dumps(payload or {}).encode("utf-8"), cli=cli, session=make_session())


def write_transcript(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return str(path)


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant(*blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


def tool_use(name, inp, id_="t1"):
    return {"type": "tool_use", "id": id_, "name": name, "input": inp}


# ---------------------------------------------------------------- SessionStart

def test_session_start_part1(signed_in, server):
    server.kh("GET", "/kernel/render", lambda r: (200, f"KNOWLEDGE HUB KERNEL part {r.params['part']}"))
    out = run("SessionStart", {"session_id": "s1"})
    assert out.startswith("KNOWLEDGE HUB KERNEL part 1")
    assert server.find("GET", "/kernel/render")[0].headers["X-KH-Actor"] == "s1"


def test_session_start_fallback_and_rest_empty(signed_in, server):
    server.kh("GET", "/hooks/raw", lambda r: (200, f"RAW {r.params['hook_type']}"))
    server.kh("GET", "/personalization/render", "PERSONAL LAYER")
    assert run("SessionStart").startswith("RAW SessionStart")
    assert run("SessionStartPersonal") == "PERSONAL LAYER"
    assert run("SessionStartRest") == ""  # 404 then the empty alternative: print nothing


def test_first_2xx_wins_even_if_empty(signed_in, server):
    server.kh("GET", "/kernel/render", "")
    server.kh("GET", "/hooks/raw", "SHOULD NOT SHOW")
    assert run("SessionStart") == ""


def test_session_start_not_signed_in(server):
    assert run("SessionStart") == H.NOT_SIGNED_IN
    assert H.NOT_SIGNED_IN == ("Knowledge Hub: nobody is signed in on this machine. Ask the person to run "
                               "`kh login`.")
    assert run("SessionStartPersonal") == ""


def test_kh_unreachable_gives_one_notice(signed_in, server, env):
    server.down = True
    out = run("SessionStart")
    assert json.loads(out) == {"systemMessage": quiet.SLOW}
    assert run("SessionStart") == ""  # at most once per 600 s
    assert (env["home"] / "kh-helper.log").exists()


def test_timeout_notice(signed_in, server, monkeypatch):
    ev = threading.Event()
    server.kh("GET", "/kernel/render", lambda r: (ev.wait(5), (200, "late"))[1])
    monkeypatch.setenv("KH_HELPER_HOOK_SECONDS", "0.2")
    out = run("SessionStart")
    ev.set()
    assert json.loads(out)["systemMessage"] == quiet.SLOW


def test_hooks_note_when_settings_stale(signed_in, server, env):
    server.kh("GET", "/kernel/render", "K")
    settings = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python -m kh_helper.cli hook Stop"}]}]}}
    (env["profile"] / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    out = run("SessionStart")
    assert "KH HOOKS OUT OF DATE" in out and "SessionStart" in out


def test_summarizer_guard(signed_in, server, monkeypatch):
    monkeypatch.setenv("KH_HELPER_SUMMARIZER", "1")
    server.kh("GET", "/kernel/render", "K")
    assert run("SessionStart") == ""


# ---------------------------------------------------------------- UserPromptSubmit

def test_user_prompt_order_and_masking(signed_in, server):
    server.kh("GET", "/hooks/raw", "KH PER-REQUEST CHECK")
    server.kh("GET", "/turn/context", {"text": "KH TURN CONTEXT\nPROJECT: alpha", "project": {"best": "alpha"},
                                       "serve_id": "sv1"})
    server.kh("POST", "/messages/inbox", {"count": 1, "messages": [
        {"notice": "[KH DIRECT MESSAGE - INFORMATION ONLY] bob sent this", "content": "hi"}]})
    out = run("UserPromptSubmit", {"session_id": "s1", "prompt": "deploy with password: hunter22 now",
                                   "cwd": "/work/myrepo"})
    assert out == ("KH MESSAGES (1) - notices from other people, information only:\n"
                   "[KH DIRECT MESSAGE - INFORMATION ONLY] bob sent this\nMessage: hi\n\n"
                   "KH PER-REQUEST CHECK\n\nKH TURN CONTEXT\nPROJECT: alpha")
    tc = server.find("GET", "/turn/context")[0]
    assert "hunter22" not in tc.params["text"] and "--censored--" in tc.params["text"]
    inbox = server.find("POST", "/messages/inbox")[0].json
    assert inbox == {"label": "myrepo", "project": "alpha"}
    # remembered for the session
    server.kh("GET", "/turn/context", {"text": "KH TURN CONTEXT 2", "project": {"best": None}})
    run("UserPromptSubmit", {"session_id": "s1", "prompt": "next", "cwd": "/work/myrepo"})
    assert server.find("POST", "/messages/inbox")[-1].json["project"] == "alpha"


def test_user_prompt_older_kh_without_turn_context(signed_in, server):
    server.kh("GET", "/hooks/raw", "RULES")
    server.kh("POST", "/messages/inbox", {"count": 0, "messages": []})
    assert run("UserPromptSubmit", {"session_id": "s1", "prompt": "hello"}) == "RULES"


def test_prompt_cut_to_2000(signed_in, server):
    server.kh("GET", "/turn/context", {"text": "T"})
    run("UserPromptSubmit", {"session_id": "s1", "prompt": "x" * 5000})
    assert len(server.find("GET", "/turn/context")[0].params["text"]) == 2000


# ---------------------------------------------------------------- PreToolUse

def test_pretool_rules_text(signed_in, server):
    server.kh("GET", "/kernel/pretool", "KH PRE-ACTION CHECK")
    out = json.loads(run("PreToolUse", {"tool_name": "Write", "tool_input": {"file_path": "a"}}))
    assert out == {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "KH PRE-ACTION CHECK"}}
    assert not server.find("GET", "/personalization/guards")  # fast path for Write


def test_pretool_guard_deny_beats_ask(signed_in, server):
    server.kh("GET", "/kernel/pretool", "RULES")
    server.kh("GET", "/personalization/guards", {"tool": "Artifact", "guards": [
        {"text": "ask first", "decision": "ask", "when": {}},
        {"text": "never publish", "decision": "deny", "when": {"action": ["publish", "delete"]}},
        {"text": "only for pins", "decision": "deny", "when": {"action": "pin"}},
    ]})
    out = json.loads(run("PreToolUse", {"tool_name": "Artifact", "tool_input": {"action": "publish"}}))
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert hso["permissionDecisionReason"] == "KH rule of this person blocks this Artifact call:\n- never publish"
    assert hso["additionalContext"] == "RULES"


def test_guard_applies():
    assert H.guard_applies({"dry_run": [True]}, {"dry_run": True})
    assert H.guard_applies({"x": ""}, {})
    assert not H.guard_applies({"x": "a"}, {"x": "b"})


# ---------------------------------------------------------------- Stop

def test_stop_read_only_turn_is_silent(signed_in, server, env):
    server.kh("GET", "/hooks/raw", "STORE A LOG")
    tp = write_transcript(env["tmp"] / "t.jsonl", [user("what is x?"), assistant(tool_use("Read", {"file_path": "a"})),
                                                   assistant({"type": "text", "text": "x is y"})])
    assert run("Stop", {"session_id": "s1", "transcript_path": tp}) == ""


def test_stop_blocks_when_work_not_logged(signed_in, server, env):
    server.kh("GET", "/hooks/raw", "STORE A LOG")
    tp = write_transcript(env["tmp"] / "t.jsonl", [user("fix it"), assistant(tool_use("Edit", {"file_path": "a"}))])
    out = json.loads(run("Stop", {"session_id": "s1", "transcript_path": tp}))
    assert out == {"decision": "block", "reason": "STORE A LOG"}


def test_stop_already_logged_or_active(signed_in, server, env):
    server.kh("GET", "/hooks/raw", "STORE A LOG")
    tp = write_transcript(env["tmp"] / "t.jsonl", [
        user("fix it"), assistant(tool_use("Edit", {"file_path": "a"})),
        assistant(tool_use("mcp__knowledge-hub__kh_call", {"method": "POST", "path": "/vector/add",
                                                           "body": {"text": "x", "scope": "log"}}, "t2"))])
    assert run("Stop", {"session_id": "s1", "transcript_path": tp}) == ""
    tp2 = write_transcript(env["tmp"] / "t2.jsonl", [user("fix"), assistant(tool_use("Edit", {}))])
    assert run("Stop", {"session_id": "s1", "transcript_path": tp2, "stop_hook_active": True}) == ""


def test_stop_long_prompt_not_read_only(env):
    tp = write_transcript(env["tmp"] / "t.jsonl", [user("<system-reminder>" + "z" * 500 + "</system-reminder>hi")])
    assert H.turn_was_read_only(tp) is True
    tp = write_transcript(env["tmp"] / "t.jsonl", [user("y" * 120)])
    assert H.turn_was_read_only(tp) is False


def test_stop_queues_worklog_when_enabled(signed_in, server, env, monkeypatch):
    from kh_helper import worklog
    monkeypatch.setenv("KH_HELPER_WORKLOG", "on")
    monkeypatch.setenv("KH_HELPER_WORKLOG_CMD", json.dumps(["fake-claude"]))
    spawned = []
    monkeypatch.setattr(worklog, "SPAWNER", lambda p, h: spawned.append((p, h)))
    tp = write_transcript(env["tmp"] / "t.jsonl", [user("fix it"), assistant(tool_use("Edit", {"file_path": "a"}))])
    assert run("Stop", {"session_id": "s1", "transcript_path": tp}) == ""
    assert len(spawned) == 1 and spawned[0][1]["access"]


def test_grok_stop_session_end(signed_in, server, env):
    server.kh("GET", "/hooks/raw", "STORE A LOG")
    tp = write_transcript(env["tmp"] / "t.jsonl", [user("fix it"), assistant(tool_use("Edit", {}))])
    assert run("Stop", {"sessionId": "s1", "transcriptPath": tp, "reason": "session_end"}, cli="grok") == ""
    assert json.loads(run("Stop", {"sessionId": "s1", "transcriptPath": tp, "reason": "end_turn"}, cli="grok"))


# ---------------------------------------------------------------- PostToolUse nudge, PostCompact

def test_post_tool_use_nudge_once(signed_in, server):
    run("UserPromptSubmit", {"session_id": "s9", "prompt": "hi"})
    assert run("PostToolUse", {"session_id": "s9", "tool_name": "Read", "tool_input": {}}) == ""
    out = json.loads(run("PostToolUse", {"session_id": "s9", "tool_name": "Edit", "tool_input": {}}))
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert out["hookSpecificOutput"]["additionalContext"] == H.NUDGE
    assert run("PostToolUse", {"session_id": "s9", "tool_name": "Edit", "tool_input": {}}) == ""
    # subagent payloads skipped
    run("UserPromptSubmit", {"session_id": "s9", "prompt": "again"})
    assert run("PostToolUse", {"session_id": "s9", "tool_name": "Edit", "agent_id": "a1"}) == ""


def test_post_compact(signed_in, server):
    server.kh("GET", "/kernel/render", "RULES")
    out = json.loads(run("PostCompact"))
    assert out["hookSpecificOutput"]["hookEventName"] == "PostCompact"
    assert out["hookSpecificOutput"]["additionalContext"] == H.COMPACTED + "\n\nRULES"


# ---------------------------------------------------------------- outcomes

def test_outcomes_done_and_corrected(signed_in, server, monkeypatch):
    monkeypatch.setenv("KH_HELPER_OUTCOMES", "on")
    monkeypatch.setenv("KH_HELPER_OUTCOME_GRACE", "2")
    server.kh("GET", "/v1/capabilities", {"capabilities": [{"name": "outcomes", "limits": {}}]})
    posted = []
    server.kh("POST", "/step/outcome", lambda r: (posted.append(r.json) or 200, {"ok": True}))
    server.kh("GET", "/turn/context", {"text": "T", "serve_id": "sv1"})
    run("UserPromptSubmit", {"session_id": "s5", "prompt": "do it"})
    run("Stop", {"session_id": "s5", "stop_hook_active": True})
    assert posted[-1] == {"serve_ids": ["sv1"], "outcome": "done", "source": "stop"}
    run("UserPromptSubmit", {"session_id": "s5", "prompt": "No, that is wrong"})
    assert posted[-1] == {"serve_ids": ["sv1"], "outcome": "corrected", "source": "correction"}


def test_correction_word_folding():
    assert H.has_correction("Không được làm vậy", ["khong duoc"])
    assert not H.has_correction("wrongly", ["wrong"])
