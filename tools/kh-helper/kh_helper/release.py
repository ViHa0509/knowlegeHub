"""Versioning, release lookup and self-upgrade.

Publishing: upload the release zip to a public link and overwrite KH row id 'ver_kh-helper' (project
'orchestrator', scope 'versions') metadata {version, download_url, sha256}. No KH redeploy is needed.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
import sys
import tempfile
import zipfile

from . import __version__

MODULE = "kh-helper"
VERSION_ROW = "ver_kh-helper"


def installed_version() -> str:
    try:
        from importlib.metadata import version
        return version(MODULE)
    except Exception:  # noqa: BLE001
        return __version__


def _key(v: str) -> list[int]:
    return [int(p) if p.isdigit() else -1 for p in re.split(r"[.+-]", str(v or "").strip().lstrip("vV"))]


def newer(a: str, b: str) -> bool:
    """Is version a newer than b?"""
    ka, kb = _key(a), _key(b)
    n = max(len(ka), len(kb))
    ka += [0] * (n - len(ka))
    kb += [0] * (n - len(kb))
    return ka > kb


def latest(session) -> dict | None:
    """{version, download_url, sha256?} of the published helper, or None."""
    try:
        st, body = session.call("POST", "/vector/scroll",
                                {"scope": "versions", "project": "orchestrator", "ids": [VERSION_ROW]})
        if st < 300 and isinstance(body, dict):
            for row in body.get("hits") or []:
                meta = (row or {}).get("metadata") or {}
                if meta.get("version") and meta.get("download_url"):
                    return {"version": meta["version"], "download_url": meta["download_url"],
                            "sha256": meta.get("sha256")}
    except Exception:  # noqa: BLE001
        pass
    try:
        cfg = session.discover()
        if cfg.get("helper_version") and cfg.get("helper"):
            url = cfg["helper"]
            if url.startswith("/"):
                url = session.kh_url + url
            return {"version": cfg["helper_version"], "download_url": url, "sha256": None}
    except Exception:  # noqa: BLE001
        pass
    return None


def drive_direct(url: str) -> str:
    """Rewrite a Google Drive share link to the direct-download form."""
    m = re.search(r"drive\.google\.com/(?:file/d/([^/?#]+)|open\?id=([^&#]+)|uc\?(?:.*&)?id=([^&#]+))", url)
    if m:
        fid = next(g for g in m.groups() if g)
        return f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"
    return url


def _download(session, source: str) -> bytes:
    if os.path.exists(source):
        with open(source, "rb") as fh:
            return fh.read()
    r = session.http.get(drive_direct(source), timeout=300)
    r.raise_for_status()
    return r.content


def upgrade(session, source: str | None = None, force: bool = False, run=subprocess.run) -> dict:
    cur = installed_version()
    info = latest(session) if not source else {"version": None, "download_url": source, "sha256": None}
    if not info:
        return {"upgraded": False, "reason": "no published version found"}
    if info.get("version") and not force and not newer(info["version"], cur):
        return {"upgraded": False, "reason": "up to date", "installed": cur, "published": info["version"]}
    data = _download(session, info["download_url"])
    if not data.startswith(b"PK"):
        return {"upgraded": False, "reason": "download is not a zip or wheel"}
    if info.get("sha256") and hashlib.sha256(data).hexdigest().lower() != str(info["sha256"]).lower():
        return {"upgraded": False, "reason": "sha256 mismatch"}
    tmp = tempfile.mkdtemp(prefix="kh-helper-")
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    if any(n.endswith(".dist-info/METADATA") for n in names) and not any(n.endswith(".whl") for n in names):
        wheel = os.path.join(tmp, f"kh_helper-{info.get('version') or 'latest'}-py3-none-any.whl")
        with open(wheel, "wb") as fh:
            fh.write(data)
    else:
        wheels = [n for n in names if re.search(r"(^|/)kh_helper-[^/]*\.whl$", n)]
        if len(wheels) != 1:
            return {"upgraded": False, "reason": "release zip must hold exactly one kh_helper wheel"}
        wheel = os.path.join(tmp, os.path.basename(wheels[0]))
        with open(wheel, "wb") as fh:
            fh.write(zf.read(wheels[0]))
    r = run([sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", wheel], capture_output=True, text=True)
    if r.returncode != 0:
        return {"upgraded": False, "reason": "pip failed", "detail": (r.stderr or "")[-600:]}
    s = run([sys.executable, "-m", "kh_helper.cli", "setup"], capture_output=True, text=True)
    return {"upgraded": True, "from": cur, "to": info.get("version"), "setup": (s.stdout or "").strip()[-2000:],
            "next": "restart this app so the new hooks and MCP server load"}
