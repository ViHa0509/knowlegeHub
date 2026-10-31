import json
import os
import subprocess
import time

from conftest import make_session
from kh_helper import worklog as W


class FakeRunner:
    def __init__(self, answer, code=0):
        self.answer, self.code, self.calls = answer, code, []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        out = json.dumps({"type": "result", "is_error": False, "result": self.answer})
        return subprocess.CompletedProcess(cmd, self.code, out.encode("utf-8"), b"")


def transcript(env):
    rows = [{"type": "user", "message": {"role": "user", "content": "rename foo to bar, token=abcd1234efgh"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Done."},
                {"type": "tool_use", "id": "t", "name": "Edit", "input": {"file_path": "x.py"}}]}}]
    p = env["tmp"] / "turn.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return str(p)


def test_enabled_and_builtin_command(env, monkeypatch):
    assert W.enabled("claude-code") is False  # KH_HELPER_WORKLOG=off in tests
    monkeypatch.setenv("KH_HELPER_WORKLOG", "on")
    assert W.enabled("claude-code") is False  # no claude on PATH (faked)
    monkeypatch.setattr(W.shutil, "which", lambda n: "/bin/claude")
    cmd = W.command("claude-code")
    assert cmd[:4] == ["/bin/claude", "-p", "--model", "haiku"]
    assert cmd[cmd.index("--system-prompt") + 1] == W.SYSTEM and "--strict-mcp-config" in cmd
    assert W.command("grok") is None
    monkeypatch.setenv("KH_HELPER_WORKLOG_CMD", '["my-llm", "--json"]')
    assert W.command("grok") == ["my-llm", "--json"]


def test_queue_and_drain_store(signed_in, server, env, monkeypatch):
    monkeypatch.setenv("KH_HELPER_WORKLOG", "on")
    monkeypatch.setenv("KH_HELPER_WORKLOG_CMD", json.dumps(["fake-claude", "-p"]))
    handed = {}
    monkeypatch.setattr(W, "SPAWNER", lambda path, h: handed.update(path=path, h=h))
    sess = make_session()
    assert W.queue(sess, {"session_id": "sess-1", "transcript_path": transcript(env), "cwd": "/w"}, "claude-code",
                   "alpha")
    job = json.loads(open(handed["path"], encoding="utf-8").read())
    assert job["project"] == "alpha" and job["session"] == "sess-1" and job["acting"] is False
    assert "access" not in json.dumps(job)
    assert handed["h"]["access"] and handed["h"]["until"] > time.time()

    server.kh("GET", "/auth/me", {"email": "ann@example.com", "personal_project": "personal-ann-example-com"})
    server.kh("GET", "/policy/plan", {"rules": [{"sub_action": "gate", "content": "GATE RULE"}]})
    server.kh("POST", "/vector/add", {"status": "ok", "vector_id": "vec_9"})
    runner = FakeRunner('{"skip": false, "subject": "rename foo", "text": "Renamed foo to bar in x.py."}')
    monkeypatch.setattr(W, "RUNNER", runner)
    # worker gets a long-lived token (>= JOB_MARGIN)
    handoff = {**handed["h"], "until": time.time() + 600}
    n = W.drain(handoff, sleep=lambda s: None)
    assert n == 1
    prompt = runner.calls[0][1]["input"].decode("utf-8")
    assert prompt.startswith("RULE: GATE RULE") and "TURN:\nPERSON: rename foo to bar" in prompt
    assert "abcd1234efgh" not in prompt and "[masked]" in prompt
    assert "TOOL Edit:" in prompt
    add = server.find("POST", "/vector/add")[0]
    assert add.headers["X-KH-Actor"] == "sess-1"
    body = add.json
    assert body["scope"] == "log" and body["text"].endswith(" Renamed foo to bar in x.py.")
    assert body["text"][:4].isdigit() and body["text"][10] == " "
    m = body["metadata"]
    assert m["project"] == "alpha" and m["subject"] == m["topic"] == "rename foo"
    assert m["source"] == "kh-helper auto worklog" and m["scope"] == "log"
    assert server.find("GET", "/policy/plan")[0].params == {"action": "store worklog", "project": "alpha"}
    assert not os.listdir(W.jobs_dir())


def test_skip_and_403_fallback(signed_in, server, env, monkeypatch):
    monkeypatch.setenv("KH_HELPER_WORKLOG_CMD", json.dumps(["fake"]))
    server.kh("GET", "/auth/me", {"personal_project": "personal-ann"})
    server.kh("GET", "/policy/plan", {}, status=404)
    seen = []

    def add(r):
        seen.append(r.json)
        return (403, {"detail": "no"}) if r.json["metadata"]["project"] == "beta" else (200, {"vector_id": "v"})
    server.kh("POST", "/vector/add", add)
    os.makedirs(W.jobs_dir(), exist_ok=True)
    for i, proj in enumerate(["beta", "orchestrator"]):
        with open(os.path.join(W.jobs_dir(), f"s-{i}.job"), "w", encoding="utf-8") as fh:
            json.dump({"transcript": transcript(env), "session": "s", "cli": "claude-code", "project": proj,
                       "at": "2026-10-31T10:00:00Z", "acting": False}, fh)
    runner = FakeRunner('{"skip": false, "subject": "x", "text": "Did x."}')
    monkeypatch.setattr(W, "RUNNER", runner)
    sess_token = make_session().access_token()
    assert W.drain({"access": sess_token, "until": time.time() + 600}, sleep=lambda s: None) == 2
    assert runner.calls and W.RULES_FALLBACK in runner.calls[0][1]["input"].decode()
    assert seen[0]["metadata"]["project"] == "beta"
    assert seen[1]["metadata"]["project"] == "personal-ann" and seen[1]["metadata"]["intended_project"] == "beta"
    assert seen[1]["text"] == "2026-10-31 Did x."
    assert seen[2]["metadata"]["project"] == "personal-ann"  # orchestrator keeps no logs
    # skip answer: nothing stored, job removed
    with open(os.path.join(W.jobs_dir(), "s-9.job"), "w", encoding="utf-8") as fh:
        json.dump({"transcript": transcript(env), "session": "s", "at": "2026-10-31T10:00:00Z"}, fh)
    monkeypatch.setattr(W, "RUNNER", FakeRunner('{"skip": true}'))
    assert W.drain({"access": sess_token, "until": time.time() + 600}, sleep=lambda s: None) == 0
    assert not os.listdir(W.jobs_dir())


def test_short_token_keeps_job(signed_in, server, env, monkeypatch):
    monkeypatch.setenv("KH_HELPER_WORKLOG_CMD", json.dumps(["fake"]))
    os.makedirs(W.jobs_dir(), exist_ok=True)
    p = os.path.join(W.jobs_dir(), "s-1.job")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"transcript": transcript(env), "session": "s", "at": "2026-10-31T10:00:00Z"}, fh)
    assert W.drain({"access": "tok", "until": time.time() + 60}, sleep=lambda s: None) == 0
    assert os.path.exists(p)


def test_turn_slice_bounds(env):
    rows = [{"type": "user", "message": {"content": "start"}}] + [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "a" * 1000}]}}] * 30
    p = env["tmp"] / "big.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    s = W.turn_slice(str(p))
    assert len(s) <= W.SLICE_CHARS + 10 and "[...]" in s and s.startswith("PERSON: start")
