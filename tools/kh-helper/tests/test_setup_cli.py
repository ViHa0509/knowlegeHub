import io
import json
import os
import sys

import pytest

from conftest import KH
from kh_helper import cli, clis
from kh_helper import hooks as H


def settings(env):
    return json.loads((env["profile"] / "settings.json").read_text(encoding="utf-8"))


def test_setup_idempotent_and_keeps_user_hooks(env, server):
    own = {"hooks": [{"type": "command", "command": "echo mine"}]}
    legacy = {"hooks": [{"type": "command", "command": "curl http://localhost:9000/hooks/raw?hook_type=Stop"}]}
    (env["profile"] / "settings.json").write_text(json.dumps({"theme": "dark", "hooks": {"Stop": [own, legacy]}}),
                                                  encoding="utf-8")
    sess = cli.S.Session(KH)
    code, res = cli.do_setup(sess)
    assert code == 0 and res["cli"] == "claude-code"
    assert res["legacy_hooks_replaced"] == 1 and res["backup"] and os.path.exists(res["backup"])
    assert res["mcp_server"]["registered"] is False
    assert "mcp add -s user knowledge-hub --" in res["mcp_server"]["manual"]
    assert res["mcp_server"]["manual"].endswith(f"-m kh_helper.cli --kh-url {KH} mcp")
    first = settings(env)
    code, res2 = cli.do_setup(sess)
    assert settings(env) == first and res2["hooks_removed"] == len(H.HOOKS)
    assert first["theme"] == "dark"
    assert first["hooks"]["Stop"][0] == own
    ss = first["hooks"]["SessionStart"]
    assert [g["hooks"][0]["command"].split()[-1] for g in ss] == ["SessionStart", "SessionStartPersonal",
                                                                  "SessionStartRest"]
    pre = first["hooks"]["PreToolUse"][0]
    assert pre["matcher"] == "Agent|Task|Write|Edit|NotebookEdit|Artifact"
    assert pre["hooks"][0]["timeout"] == 30 and pre["hooks"][0]["type"] == "command"
    assert first["hooks"]["PostToolUse"][0]["matcher"] == H.POST_MATCHER
    assert len(first["hooks"]["PostCompact"]) == 3
    assert H.stale_events(first) == []
    # remove: only ours go, the person's own hook stays
    code, res3 = cli.do_setup(sess, remove=True)
    assert settings(env)["hooks"] == {"Stop": [own]}


def test_setup_refusals(env, server, monkeypatch, tmp_path):
    monkeypatch.setenv("KH_HELPER_HOME", str(tmp_path / "elsewhere" / "kh-helper"))
    code, res = cli.do_setup(cli.S.Session(KH))
    assert code == 3 and "not inside a known CLI profile" in res["error"]
    # Claude Code may not set up a Grok copy
    monkeypatch.setenv("KH_HELPER_HOME", str(env["user"] / ".grok" / "kh-helper"))
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
    code, res = cli.do_setup(cli.S.Session(KH))
    assert code == 3 and "Grok" in res["error"]


def test_setup_codex_toml(env, server, monkeypatch):
    monkeypatch.setenv("KH_HELPER_HOME", str(env["user"] / ".codex" / "kh-helper"))
    cfg = env["user"] / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('model = "x"\n\n[mcp_servers.knowledge-hub]\ncommand = "old"\n[mcp_servers.knowledge-hub.env]\n'
                   'A = "1"\n\n[other]\nb = 2\n', encoding="utf-8")
    code, res = cli.do_setup(cli.S.Session(KH))
    text = cfg.read_text(encoding="utf-8")
    assert code == 0 and text.count("[mcp_servers.knowledge-hub]") == 1 and '"old"' not in text
    assert "[other]" in text and '"--kh-url"' in text and clis.registered("codex", "knowledge-hub")


def test_cli_call_and_exit_codes(signed_in, server, capsys, monkeypatch):
    server.kh("POST", "/vector/query", lambda r: (200, {"hits": [], "echo": r.json}))
    assert cli.main(["call", "POST", "/vector/query", '{"text": "xin chào"}', "--kh-url", KH]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["echo"] == {"text": "xin chào"}
    server.kh("GET", "/nope", {"detail": "no"}, status=403)
    assert cli.main(["--kh-url", KH, "call", "GET", "/nope"]) == 1
    capsys.readouterr()
    # stdin body, utf-8 with BOM
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO('\ufeff{"text": "đã"}'.encode("utf-8"))))
    assert cli.main(["call", "POST", "/vector/query", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["echo"] == {"text": "đã"}


def test_cli_not_signed_in_exit_2(server, capsys):
    assert cli.main(["--kh-url", KH, "me"]) == 2


def test_cli_hook_never_fails(server, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"not json")))
    assert cli.main(["hook", "SessionStart", "--kh-url", KH]) == 0
    assert H.NOT_SIGNED_IN in capsys.readouterr().out


def test_cli_status_and_message(signed_in, server, capsys):
    assert cli.main(["status"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["signed_in"] is True and st["kh_url"] == KH
    server.kh("POST", "/messages", {"id": 1})
    assert cli.main(["message", "send", "alpha", "bob@x.com", "hello"]) == 0
    assert server.find("POST", "/messages")[0].json == {"project": "alpha", "receiver": "bob@x.com",
                                                        "content": "hello"}


def test_hook_exe_unquoted_without_spaces(monkeypatch):
    monkeypatch.setattr(sys, "executable", r"C:\venv\Scripts\python.exe")
    if os.name == "nt":
        assert H.hook_exe() == "C:/venv/Scripts/python.exe"
        assert H.hook_command("Stop") == "C:/venv/Scripts/python.exe -m kh_helper.cli hook Stop"


def test_register_mcp_claude_with_fake_cli(env):
    calls = []

    class R:
        returncode = 0

    def run(argv, **kw):
        calls.append((argv, kw["env"].get("CLAUDE_CONFIG_DIR"), "CLAUDECODE" in kw["env"]))
        return R()
    res = H.register_mcp_claude(str(env["profile"]), "knowledge-hub", ["py", "-m", "kh_helper.cli", "mcp"],
                                which=lambda n: "fake-claude", run=run)
    assert res["registered"] is True
    assert calls[0][0] == ["fake-claude", "mcp", "remove", "-s", "user", "knowledge-hub"]
    assert calls[1][0] == ["fake-claude", "mcp", "add", "-s", "user", "knowledge-hub", "--", "py", "-m",
                           "kh_helper.cli", "mcp"]
    assert all(c[1] == str(env["profile"]) and c[2] is False for c in calls)
