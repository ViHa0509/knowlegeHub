import asyncio
import importlib.util
import json
import os
import time
import zipfile

from conftest import ISSUER, make_session
from kh_helper import __version__, mcp_server
from kh_helper import session as S
from test_mcp import _text

DESKTOP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "desktop")


def _builder():
    spec = importlib.util.spec_from_file_location("build_mcpb", os.path.join(DESKTOP, "build_mcpb.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_desktop_tool_list(server):
    srv = mcp_server.build(make_session(), instructions_text="I", desktop=True)
    names = sorted(t.name for t in asyncio.run(srv.list_tools()))
    assert names == sorted(mcp_server.DESKTOP_TOOL_NAMES) and len(names) == 17


def test_desktop_instructions_note(signed_in, server, monkeypatch):
    monkeypatch.setenv("KH_HELPER_STARTUP_SECONDS", "0")
    assert mcp_server.instructions(make_session(), desktop=True).endswith(mcp_server.DESKTOP_NOTE)


def test_kh_login_returns_link_without_waiting(server, monkeypatch):
    monkeypatch.setattr(S.webbrowser, "open", lambda url: True)
    monkeypatch.setattr(S, "LOGIN_WAIT_SECONDS", 3)
    srv = mcp_server.build(make_session(), instructions_text="I", desktop=True)
    t0 = time.monotonic()
    res = json.loads(_text(asyncio.run(srv.call_tool("kh_login", {}))))
    assert time.monotonic() - t0 < 5
    assert res["state"] == "waiting" and res["link"].startswith(ISSUER + "/protocol/openid-connect/auth?")
    again = json.loads(_text(asyncio.run(srv.call_tool("kh_login", {}))))
    assert again == res


def test_kh_login_signed_in_and_logout(server, monkeypatch):
    sess = make_session()
    monkeypatch.setattr(sess, "login", lambda: {"email": "ann@example.com"})
    monkeypatch.setattr(sess, "logout", lambda: "signed out")
    srv = mcp_server.build(sess, instructions_text="I", desktop=True)
    res = json.loads(_text(asyncio.run(srv.call_tool("kh_login", {}))))
    assert res == {"state": "signed_in", "email": "ann@example.com"}
    res = json.loads(_text(asyncio.run(srv.call_tool("kh_logout", {}))))
    assert res == {"logout": "signed out"}


def test_build_mcpb(tmp_path):
    path = _builder().build(str(tmp_path))
    assert os.path.basename(path) == f"knowledge-hub-{__version__}.mcpb"
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        manifest = json.loads(z.read("manifest.json"))
        pyproject = z.read("pyproject.toml").decode()
    assert {"manifest.json", "pyproject.toml", "src/server.py", "src/kh_helper/__init__.py",
            "src/kh_helper/mcp_server.py"} <= set(names)
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)
    assert manifest["version"] == __version__ and manifest["server"]["entry_point"] == "src/server.py"
    assert f'version = "{__version__}"' in pyproject
