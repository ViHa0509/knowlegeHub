"""Which CLI a copy of the helper belongs to, and MCP registration for CLIs other than Claude Code.

Claude Code gets hooks + MCP (hooks.setup_claude); Grok gets PreToolUse/Stop hooks + MCP; Codex and Gemini get
only the MCP server. Hook-less CLIs get the SessionStart rules in the MCP instructions and use kh_context.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sys
import time

SERVER_NAME = "knowledge-hub"
CLAUDE = "claude-code"
GROK = "grok"
CODEX = "codex"
GEMINI = "gemini"
CLIS = (CLAUDE, GROK, CODEX, GEMINI)
NAMES = {CLAUDE: "Claude Code", GROK: "Grok CLI", CODEX: "Codex CLI", GEMINI: "Gemini CLI"}
PREFIXES = {".claude": CLAUDE, ".grok": GROK, ".codex": CODEX, ".gemini": GEMINI}


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(path)))


def profile(cli: str = CLAUDE) -> str:
    """The CLI's profile folder."""
    if cli == CLAUDE:
        return os.path.abspath(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))
    if cli == GROK:
        return os.path.abspath(os.path.expanduser(os.environ.get("GROK_HOME") or "~/.grok"))
    if cli == CODEX:
        return os.path.abspath(os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"))
    if cli == GEMINI:
        return os.path.abspath(os.path.expanduser("~/.gemini"))
    raise ValueError(f"unknown cli {cli}")


def is_default_claude_profile(path: str) -> bool:
    return _norm(path) == _norm("~/.claude")


def cli_of_home(home: str) -> str | None:
    """The CLI whose profile equals HOME's parent, else by folder-name prefix."""
    parent = os.path.dirname(os.path.abspath(home))
    for cli in CLIS:
        try:
            if _norm(profile(cli)) == _norm(parent):
                return cli
        except Exception:  # noqa: BLE001
            continue
    name = os.path.basename(parent).lower()
    for prefix, cli in PREFIXES.items():
        if name.startswith(prefix):
            return cli
    return None


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def backup(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    dest = f"{path}.kh-bak-{stamp()}"
    shutil.copy2(path, dest)
    return dest


def delete_backups(paths: list[str]) -> list[str]:
    """Delete every '<file>.kh-bak-*' next to the given files (used by uninstall)."""
    gone = []
    for p in paths:
        for b in glob.glob(glob.escape(p) + ".kh-bak-*"):
            try:
                os.remove(b)
                gone.append(b)
            except OSError:
                pass
    return gone


def mcp_args(kh_url: str) -> list[str]:
    return ["-m", "kh_helper.cli", "--kh-url", kh_url, "mcp"]


# ---------------------------------------------------------------- TOML (Grok, Codex)

def _toml_remove(text: str, name: str) -> str:
    """Remove the [mcp_servers.<name>] table and its sub-tables, line-wise."""
    out, skipping = [], False
    head = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*$")
    targets = {f"mcp_servers.{name}", f'mcp_servers."{name}"'}
    for line in text.splitlines(keepends=True):
        m = head.match(line)
        if m:
            key = m.group(1).strip()
            skipping = any(key == t or key.startswith(t + ".") for t in targets)
        if not skipping:
            out.append(line)
    return "".join(out)


def toml_register(path: str, name: str, command: str, args: list[str] | None, remove: bool = False) -> dict:
    text = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    bak = backup(path)
    text = _toml_remove(text, name)
    if not remove:
        if text and not text.endswith("\n"):
            text += "\n"
        if text.strip():
            text += "\n"
        text += f"[mcp_servers.{name}]\n"
        text += f"command = {json.dumps(command)}\n"
        text += "args = [" + ",".join(json.dumps(a) for a in (args or [])) + "]\n"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return {"config": path, "backup": bak, "server": name, "removed" if remove else "registered": True}


def json_register(path: str, name: str, command: str, args: list[str] | None, remove: bool = False) -> dict:
    data: dict = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh) or {}
    bak = backup(path)
    servers = data.setdefault("mcpServers", {})
    servers.pop(name, None)
    if not remove:
        servers[name] = {"command": command, "args": list(args or [])}
    if not servers:
        data.pop("mcpServers", None)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return {"config": path, "backup": bak, "server": name, "removed" if remove else "registered": True}


def config_file(cli: str) -> str:
    if cli in (GROK, CODEX):
        return os.path.join(profile(cli), "config.toml")
    if cli == GEMINI:
        return os.path.join(profile(cli), "settings.json")
    raise ValueError(cli)


def register(cli: str, name: str, command: str, args: list[str] | None, remove: bool = False) -> dict:
    """Register (or remove) an MCP server for Grok / Codex / Gemini."""
    path = config_file(cli)
    if cli == GEMINI:
        return json_register(path, name, command, args, remove)
    return toml_register(path, name, command, args, remove)


def registered(cli: str, name: str) -> bool:
    """Is MCP server <name> registered in this CLI's config?"""
    try:
        if cli == CLAUDE:
            prof = profile(CLAUDE)
            files = [os.path.join(prof, ".claude.json")]
            if is_default_claude_profile(prof):
                files.append(os.path.expanduser("~/.claude.json"))
            for f in files:
                if os.path.exists(f):
                    with open(f, encoding="utf-8") as fh:
                        if name in ((json.load(fh) or {}).get("mcpServers") or {}):
                            return True
            return False
        path = config_file(cli)
        if not os.path.exists(path):
            return False
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if cli == GEMINI:
            return name in ((json.loads(text or "{}") or {}).get("mcpServers") or {})
        return re.search(r"^\s*\[mcp_servers\.\"?" + re.escape(name) + r"\"?\]", text, re.M) is not None
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- Grok hooks

def grok_hooks_file() -> str:
    return os.path.join(profile(GROK), "hooks", "knowledge-hub.json")


def setup_grok(kh_url: str, remove: bool = False, no_mcp: bool = False) -> dict:
    """Grok: only PreToolUse and Stop hooks (it ignores SessionStart/UserPromptSubmit output)."""
    from . import hooks
    path = grok_hooks_file()
    bak = backup(path)
    out: dict = {"hooks_file": path, "backup": bak}
    if remove:
        if os.path.exists(path):
            os.remove(path)
        out["hooks_removed"] = True
    else:
        exe = hooks.hook_exe()
        data = {"hooks": {}}
        for key in ("PreToolUse", "Stop"):
            spec = hooks.HOOKS[key]
            group: dict = {}
            if spec.get("matcher"):
                group["matcher"] = spec["matcher"]
            group["hooks"] = [{"type": "command",
                               "command": f"{exe} -m kh_helper.cli hook {key} --cli grok", "timeout": 30}]
            data["hooks"].setdefault(spec.get("event") or key, []).append(group)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        out["hooks_added"] = ["PreToolUse", "Stop"]
    if not no_mcp:
        out["mcp_server"] = register(GROK, SERVER_NAME, sys.executable, mcp_args(kh_url), remove)
    return out


def setup_other(cli: str, kh_url: str, remove: bool = False, no_mcp: bool = False) -> dict:
    """Codex / Gemini: MCP server only."""
    if no_mcp:
        return {"mcp_server": None}
    return {"mcp_server": register(cli, SERVER_NAME, sys.executable, mcp_args(kh_url), remove)}
