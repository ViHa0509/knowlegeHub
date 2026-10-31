"""`kh` command line.

Exit codes: 0 ok, 1 KH error, 2 not signed in, 3 wrong owner, 4 password not changed.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys

from . import __version__, clis, owner, quiet
from . import session as S

EXIT_OK, EXIT_KH, EXIT_SIGNIN, EXIT_OWNER, EXIT_PASSWORD = 0, 1, 2, 3, 4


def _out(data) -> None:
    if isinstance(data, str):
        text = data
    else:
        text = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    sys.stdout.flush()


def _err(msg: str) -> None:
    sys.stderr.write(msg + "\n")


def read_stdin_text() -> str:
    """stdin as raw bytes decoded utf-8-sig (Windows code pages break non-ASCII text otherwise)."""
    data = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode("utf-8")
    return data.decode("utf-8-sig", "replace")


def _split_kh_url(argv: list[str]) -> tuple[str | None, list[str]]:
    """--kh-url is accepted before or after the subcommand."""
    url, rest, i = None, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--kh-url" and i + 1 < len(argv):
            url = argv[i + 1]
            i += 2
            continue
        if a.startswith("--kh-url="):
            url = a.split("=", 1)[1]
            i += 1
            continue
        rest.append(a)
        i += 1
    return url, rest


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kh", description="Knowledge Hub helper")
    p.add_argument("--version", action="version", version=f"kh-helper {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login")
    lg.add_argument("--no-browser", action="store_true")
    sub.add_parser("password")
    sub.add_parser("logout")
    sub.add_parser("status")
    sub.add_parser("me")
    c = sub.add_parser("call")
    c.add_argument("method")
    c.add_argument("path")
    c.add_argument("body", nargs="?")
    m = sub.add_parser("members")
    m.add_argument("action", choices=["people", "list", "add", "remove", "exclude", "include", "exclusions"])
    m.add_argument("project")
    m.add_argument("email", nargs="?", default="")
    m.add_argument("--level", choices=["owner", "editable", "readonly"], default="readonly")
    sub.add_parser("catalog")
    msg = sub.add_parser("message")
    msub = msg.add_subparsers(dest="mcmd", required=True)
    ms = msub.add_parser("send")
    ms.add_argument("project")
    ms.add_argument("receiver")
    ms.add_argument("text")
    msub.add_parser("inbox")
    sub.add_parser("mcp")
    st = sub.add_parser("setup")
    st.add_argument("--remove", action="store_true")
    st.add_argument("--settings")
    st.add_argument("--no-mcp", action="store_true")
    sub.add_parser("uninstall")
    h = sub.add_parser("hook")
    h.add_argument("event")
    h.add_argument("--cli", choices=["claude-code", "grok"], default="claude-code")
    sub.add_parser("worklog")
    up = sub.add_parser("upgrade")
    up.add_argument("--from", dest="source")
    up.add_argument("--force", action="store_true")
    ar = sub.add_parser("artifact")
    asub = ar.add_subparsers(dest="acmd", required=True)
    au = asub.add_parser("upload")
    au.add_argument("file")
    au.add_argument("--module", default="")
    au.add_argument("--version", dest="aversion", default="")
    asub.add_parser("list")
    return p


def _body(arg: str | None):
    if arg is None:
        return None
    if arg == "-":
        text = read_stdin_text()
    elif arg.startswith("@"):
        with open(arg[1:], "rb") as fh:
            text = fh.read().decode("utf-8-sig")
    else:
        text = arg
    text = text.strip()
    return json.loads(text) if text else None


def _show(status: int, body) -> int:
    _out(body if not isinstance(body, str) else body)
    return EXIT_OK if status < 300 else EXIT_KH


def do_setup(session, remove: bool = False, settings: str | None = None, no_mcp: bool = False) -> tuple[int, dict]:
    h = S.home()
    why = owner.setup_refusal(h, remove=remove)
    if why:
        return EXIT_OWNER, {"error": why}
    cli = clis.cli_of_home(h) or clis.CLAUDE
    from . import hooks
    if cli == clis.CLAUDE:
        res = hooks.setup_claude(session.kh_url, remove=remove, settings=settings, no_mcp=no_mcp)
    elif cli == clis.GROK:
        res = clis.setup_grok(session.kh_url, remove=remove, no_mcp=no_mcp)
    else:
        res = clis.setup_other(cli, session.kh_url, remove=remove, no_mcp=no_mcp)
    res["cli"] = cli
    if remove:
        res["signed_out"] = session.logout_all()
    return EXIT_OK, res


def _documented_install(h: str) -> bool:
    return (os.path.normcase(os.path.abspath(sys.prefix)) == os.path.normcase(os.path.abspath(h))
            and os.path.basename(os.path.abspath(h)) == "kh-helper"
            and os.path.exists(os.path.join(h, "pyvenv.cfg")))


def _delete_home_later(h: str) -> None:
    """Windows: a detached hidden pythonw of the base Python retries rmtree every 2 s for up to 1 h."""
    code = ("import shutil,time,os,sys\n"
            "p=sys.argv[1]\n"
            "for _ in range(1800):\n"
            "    shutil.rmtree(p, ignore_errors=True)\n"
            "    if not os.path.exists(p): break\n"
            "    time.sleep(2)\n")
    base = os.path.join(sys.base_prefix, "pythonw.exe")
    if not os.path.exists(base):
        base = os.path.join(sys.base_prefix, "python.exe")
    subprocess.Popen([base, "-c", code, h], cwd=os.path.expanduser("~"),
                     creationflags=0x08000000 | 0x00000008 | 0x00000200,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def do_uninstall(session) -> tuple[int, dict]:
    code, res = do_setup(session, remove=True)
    from . import hooks
    files = [hooks.settings_path()] + [clis.config_file(c) for c in (clis.GROK, clis.CODEX, clis.GEMINI)]
    files.append(clis.grok_hooks_file())
    res["backups_deleted"] = clis.delete_backups(files)
    res["signed_out"] = session.logout_all()
    h = S.home()
    if _documented_install(h):
        if os.name == "nt":
            _delete_home_later(h)
            res["home"] = f"{h} will be deleted once this program exits"
        else:
            shutil.rmtree(h, ignore_errors=True)
            res["home"] = f"{h} deleted"
    else:
        res["home"] = f"{h} kept (not the documented install)"
    return code, res


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    kh_url, argv = _split_kh_url(argv)
    args = _parser().parse_args(argv)

    if args.cmd == "hook":
        try:
            from . import hooks
            raw = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read()
            out = hooks.run_hook(args.event, raw, cli=args.cli, session=S.Session(kh_url))
            if out:
                try:
                    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
                _out(out)
        except BaseException as e:  # noqa: BLE001
            quiet.log(f"hook {args.event}", e)
        return EXIT_OK

    if args.cmd == "worklog":
        try:
            from . import worklog
            raw = read_stdin_text().strip()
            handoff = json.loads(raw) if raw else {}
            n = worklog.drain(handoff)
            _out(f"Stored {n} work log(s).")
        except BaseException as e:  # noqa: BLE001
            quiet.log("worklog", e)
        return EXIT_OK

    session = S.Session(kh_url)
    try:
        if args.cmd == "login":
            res = session.login(no_browser=args.no_browser)
            _out(f"Signed in as {res.get('email')} until {res.get('until')}.")
            cli = clis.cli_of_home(S.home()) or clis.CLAUDE
            name = clis.NAMES.get(cli, cli)
            if sys.stdin.isatty() and sys.stdout.isatty():
                ans = input(f"Connect Knowledge Hub to {name} now? [Y/n] ").strip().lower()
                if ans in ("", "y", "yes"):
                    code, out = do_setup(session)
                    _out(out)
                    return code
            else:
                _out(f"To connect Knowledge Hub to {name}, run: kh setup")
            return EXIT_OK
        if args.cmd == "password":
            res = session.change_password()
            _out(f"Password changed for {res.get('email')}.")
            return EXIT_OK
        if args.cmd == "logout":
            _out({"kh_url": session.kh_url, "result": session.logout()})
            return EXIT_OK
        if args.cmd == "status":
            _out(session.status())
            return EXIT_OK
        if args.cmd == "me":
            return _show(*session.call("GET", "/auth/me"))
        if args.cmd == "call":
            return _show(*session.call(args.method.upper(), args.path, _body(args.body)))
        if args.cmd == "members":
            from . import tools
            return _show(*tools.members(session, args.project, args.action, args.email, args.level))
        if args.cmd == "catalog":
            from . import tools
            return _show(*tools.catalog(session))
        if args.cmd == "message":
            if args.mcmd == "send":
                return _show(*session.call("POST", "/messages", {"project": args.project, "receiver": args.receiver,
                                                                 "content": args.text}))
            return _show(*session.call("POST", "/messages/inbox", {}))
        if args.cmd == "mcp":
            session.check_owner()
            from . import mcp_server
            server = mcp_server.build(session)
            server.run("stdio")
            return EXIT_OK
        if args.cmd == "setup":
            code, res = do_setup(session, args.remove, args.settings, args.no_mcp)
            _out(res)
            return code
        if args.cmd == "uninstall":
            code, res = do_uninstall(session)
            _out(res)
            return code
        if args.cmd == "upgrade":
            from . import release
            _out(release.upgrade(session, args.source, args.force))
            return EXIT_OK
        if args.cmd == "artifact":
            if args.acmd == "list":
                return _show(*session.call("GET", "/artifacts"))
            with open(args.file, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("ascii")
            return _show(*session.call("POST", "/artifacts", {"name": os.path.basename(args.file),
                                                              "content_b64": b64, "module": args.module,
                                                              "version": args.aversion}))
    except S.NotSignedIn as e:
        _err(f"not signed in: {e}")
        return EXIT_SIGNIN
    except owner.WrongOwner as e:
        _err(f"wrong owner: {e}")
        return EXIT_OWNER
    except S.PasswordNotChanged as e:
        _err(f"password not changed: {e}")
        return EXIT_PASSWORD
    except S.LoginFailed as e:
        _err(f"sign-in failed: {e}")
        return EXIT_KH
    except Exception as e:  # noqa: BLE001
        quiet.log(f"cli {args.cmd}", e)
        _err(f"error: {e}")
        return EXIT_KH
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
