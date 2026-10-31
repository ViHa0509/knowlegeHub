"""Companion MCP tools installed on this machine (1.11.0), registered exactly like knowledge-hub."""
from __future__ import annotations

import json
import os

from . import clis

# name -> discovery file (relative to %LOCALAPPDATA%)
CATALOG = {
    "app-session-manager": os.path.join("AppSessionManager", "mcp.json"),
}


def discovery_path(name: str) -> str | None:
    base = os.environ.get("LOCALAPPDATA")
    if not base or name not in CATALOG:
        return None
    return os.path.join(base, CATALOG[name])


def installed() -> dict[str, dict]:
    """{name: {command, args}} for every catalog companion whose discovery file is valid."""
    found: dict[str, dict] = {}
    for name in CATALOG:
        path = discovery_path(name)
        if not path or not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh) or {}
        except (OSError, ValueError):
            continue
        cmd, args = d.get("command"), list(d.get("args") or [])
        if d.get("name") != name or not cmd or not os.path.exists(cmd):
            continue
        if args and not os.path.exists(args[0]):
            continue
        found[name] = {"command": cmd, "args": args}
    return found


def note(cli: str = clis.CLAUDE, python: str | None = None) -> str:
    missing = [n for n in installed() if not clis.registered(cli, n)]
    if not missing:
        return ""
    import sys
    py = python or sys.executable
    return (f"COMPANION TOOL NOT CONNECTED: {', '.join(missing)} is installed on this machine but this CLI has no "
            f"MCP server for it. When a task needs it, or the person agrees, run \"{py}\" -m kh_helper.cli setup "
            "and ask the person to restart this app so the tool loads.")
