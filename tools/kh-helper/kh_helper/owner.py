"""Ownership lock: a sign-in answers only the CLI that made it (introduced 1.4.0).

A Claude-owned HOME answers only Claude Code running with the same profile; an other-owned HOME never answers
Claude Code. Enforced at login, change_password, status, every token refresh and `kh mcp`. Logout is always
allowed.
"""
from __future__ import annotations

import os
import subprocess
import sys

from . import clis

PY_LAUNCHERS = {"python.exe", "pythonw.exe", "py.exe", "python", "python3"}
SHELLS = {"bash.exe", "sh.exe", "pwsh.exe", "powershell.exe", "cmd.exe", "bash", "sh", "zsh", "dash"}
CLAUDE_EXES = {"claude.exe", "claude"}
MAX_ANCESTRY = 64
MAX_LAUNCH_HOPS = 8


class WrongOwner(Exception):
    """This sign-in belongs to another CLI (or another profile)."""


# ---------------------------------------------------------------- process table

def _table_windows() -> dict[int, tuple[int, str]]:
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    table: dict[int, tuple[int, str]] = {}
    if snap in (None, wintypes.HANDLE(-1).value):
        return table
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID), entry.szExeFile)
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return table


def _table_posix() -> dict[int, tuple[int, str]]:
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=10)
    table: dict[int, tuple[int, str]] = {}
    for line in out.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            table[int(parts[0])] = (int(parts[1]), os.path.basename(parts[2].strip()))
        except ValueError:
            continue
    return table


def process_table() -> dict[int, tuple[int, str]]:
    """{pid: (ppid, exe name)}; empty on failure."""
    try:
        return _table_windows() if os.name == "nt" else _table_posix()
    except Exception:  # noqa: BLE001
        return {}


def is_ancestor(pid: int, table: dict | None = None) -> bool:
    """Is pid this process or one of its ancestors (up to 64 levels)?"""
    me = os.getpid()
    if pid == me:
        return True
    table = process_table() if table is None else table
    cur = me
    for _ in range(MAX_ANCESTRY):
        info = table.get(cur)
        if not info:
            return False
        parent = info[0]
        if parent == pid:
            return True
        if parent in (0, cur):
            return False
        cur = parent
    return False


def launched_by_claude(table: dict | None = None) -> bool:
    """Walk the parent chain (max 8 hops) over Python launchers and shells; true if it reaches claude."""
    table = process_table() if table is None else table
    cur = os.getpid()
    for _ in range(MAX_LAUNCH_HOPS):
        info = table.get(cur)
        if not info:
            return False
        parent = info[0]
        pinfo = table.get(parent)
        if not pinfo:
            return False
        name = pinfo[1].lower()
        if name in CLAUDE_EXES:
            return True
        if name in PY_LAUNCHERS or name in SHELLS:
            cur = parent
            continue
        return False
    return False


# ---------------------------------------------------------------- caller / owner

def caller() -> dict:
    """{"cli": "claude-code", "profile": ...} when Claude Code runs us, else {"cli": "other"}."""
    if os.environ.get("CLAUDECODE") == "1":
        pid = os.environ.get("CLAUDE_PID", "")
        ok = False
        try:
            ok = bool(pid) and is_ancestor(int(pid))
        except ValueError:
            ok = False
        if not ok:
            ok = launched_by_claude()
        if ok:
            return {"cli": clis.CLAUDE, "profile": clis.profile(clis.CLAUDE)}
    return {"cli": "other"}


def looks_like_claude_profile(folder: str) -> bool:
    name = os.path.basename(os.path.abspath(folder)).lower()
    if name.startswith(".claude"):
        return True
    if os.path.exists(os.path.join(folder, ".credentials.json")):
        return True
    return os.path.exists(os.path.join(folder, "settings.json")) and os.path.isdir(os.path.join(folder, "projects"))


def record_for(home: str, who: dict | None = None) -> dict:
    """The owner record written at sign-in."""
    who = who or caller()
    if who.get("cli") == clis.CLAUDE:
        return {"cli": clis.CLAUDE, "profile": who.get("profile")}
    parent = os.path.dirname(os.path.abspath(home))
    return {"cli": "other", "profile": parent, "as": clis.cli_of_home(home)}


def owner_of(home: str, cfg: dict) -> dict:
    """helper.json.owner, or the inferred owner of a pre-1.4.0 HOME."""
    rec = cfg.get("owner")
    if isinstance(rec, dict) and rec.get("cli"):
        return rec
    parent = os.path.dirname(os.path.abspath(home))
    if looks_like_claude_profile(parent):
        return {"cli": clis.CLAUDE, "profile": parent}
    return {"cli": "other", "profile": parent}


def _same(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def allowed(owner: dict, who: dict) -> bool:
    if owner.get("cli") == clis.CLAUDE:
        return who.get("cli") == clis.CLAUDE and _same(owner.get("profile"), who.get("profile"))
    if who.get("cli") == clis.CLAUDE:
        # A sign-in made from a terminal for the copy inside this Claude profile ('as' == claude-code)
        # still serves that Claude Code profile (documented install: `kh login` in a terminal).
        return owner.get("as") == clis.CLAUDE and _same(owner.get("profile"), who.get("profile"))
    return True


def login_check(home: str, who: dict | None = None) -> None:
    """Claude Code may sign in only the copy inside its own profile; a terminal may sign in any copy."""
    who = who or caller()
    if who.get("cli") != clis.CLAUDE:
        return
    if clis.cli_of_home(home) != clis.CLAUDE or not _same(os.path.dirname(os.path.abspath(home)), who.get("profile")):
        raise WrongOwner("this kh-helper copy does not belong to this Claude Code profile; sign in from its own CLI")


def check(home: str, cfg: dict, who: dict | None = None) -> None:
    """Raise WrongOwner unless this caller may use the sign-in kept in HOME."""
    if not cfg or not (cfg.get("owner") or cfg.get("signed_in") or cfg.get("signed_in_to")):
        return  # nothing signed in here yet: nothing to protect
    who = who or caller()
    owner = owner_of(home, cfg)
    if allowed(owner, who):
        return
    if owner.get("cli") == clis.CLAUDE:
        raise WrongOwner(
            f"this Knowledge Hub sign-in belongs to Claude Code (profile {owner.get('profile')}); "
            "install kh-helper into this CLI's own profile and sign in there")
    raise WrongOwner(
        "this Knowledge Hub sign-in belongs to another CLI, not Claude Code; install kh-helper into the "
        "Claude Code profile and sign in there")


def setup_refusal(home: str, who: dict | None = None, remove: bool = False) -> str | None:
    """Why `kh setup` must not run from here, or None."""
    if remove:
        return None
    who = who or caller()
    home_cli = clis.cli_of_home(home)
    if home_cli is None:
        return ("this kh-helper copy is not inside a known CLI profile (expected <profile>/kh-helper, e.g. "
                "~/.claude/kh-helper); install it there first")
    if who.get("cli") == clis.CLAUDE:
        if home_cli != clis.CLAUDE:
            return f"Claude Code may not set up the {clis.NAMES.get(home_cli, home_cli)} copy of kh-helper"
        if not _same(os.path.dirname(os.path.abspath(home)), who.get("profile")):
            return ("this kh-helper copy belongs to another Claude Code profile; run setup from Claude Code "
                    "with that profile")
    return None


def python_name() -> str:
    return os.path.basename(sys.executable)
