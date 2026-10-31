"""/auth routes (spec §2.13, §5.11): help guide, config, me, helper downloads.

    GET /auth/help                 public, text/plain; guide row `kh-guide-auth-help` in orchestrator knowledge,
                                   with {source}, {kh_url}, {issuer} replaced (plain string replace)
    PUT /auth/help {text}          maintainer -> {doc: "auth-help", id, chars}
    GET /auth/download/{filename}  public; DB download source, else uploaded artifact, else bundled wheel
    GET /auth/config               public -> {issuer, client_id, audience, help, helper, helper_version}
    GET /auth/me                   -> {email, personal_project, role, permissions, all_projects, projects}
"""
from __future__ import annotations

import glob
import logging
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from app import auth, config, personal, projects, sqldb
from app import vector_store as vs

log = logging.getLogger(__name__)

GUIDE_ID = "kh-guide-auth-help"
GUIDE_DOC = "auth-help"
HELPER_MODULE = "kh-helper"
SEED_PATH = Path(config.ROOT) / "seeds" / "auth_help.seed.txt"
DOWNLOADS_DIR = Path(config.ROOT) / "downloads"
_WHEEL_RE = re.compile(r"^kh_helper-([0-9][^-]*)-.*\.whl$")


def _guide_coll(create: bool = False):
    name = vs.part_collection_name("knowledge", projects.ORCHESTRATOR)
    return vs.get_or_create(name) if create else vs.get_existing(name)


def guide_text() -> str | None:
    h = _guide_coll()
    if h is None:
        return None
    rows = vs.get_ids_vt(h, [GUIDE_ID])
    return rows[0].get("source_text") if rows else None


def _store_guide(text: str, encode) -> None:
    vec = encode(text)
    meta = {"scope": "knowledge", "project": projects.ORCHESTRATOR, "kind": "guide", "doc": GUIDE_DOC,
            "subject_key": GUIDE_ID}
    vs.upsert_one(_guide_coll(create=True), GUIDE_ID, vec, text, meta)


def seed_guide(encode) -> bool:
    """Seed the guide row from seeds/auth_help.seed.txt if missing (never overwritten). True when written."""
    if guide_text() is not None:
        return False
    if not SEED_PATH.exists():
        log.warning("auth help seed %s missing", SEED_PATH)
        return False
    _store_guide(SEED_PATH.read_text(encoding="utf-8"), encode)
    return True


def _base_url(request: Request) -> str:
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc)
    return f"{proto}://{host.split(',')[0].strip()}"


# ---------------------------------------------------------------------------
# Download sources (§4.12 downloads table, §4 artifacts, bundled wheel)
# ---------------------------------------------------------------------------


def _db_source(filename: str | None = None, module: str | None = None) -> dict | None:
    """Row of `kh_download_source` (owned by app/downloads.py) by module or file name; None when absent."""
    try:
        with sqldb.connect() as c:
            if not sqldb.table_exists(c, "kh_download_source"):
                return None
            rows = c.execute("SELECT * FROM kh_download_source").fetchall()
    except Exception as e:  # noqa: BLE001
        log.debug("download sources unreadable: %s", e)
        return None
    for r in rows:
        d = {k: r[k] for k in ("module", "type", "path", "version", "sha256", "file_name", "notes", "updated_at")}
        if (module and d["module"] == module) or (filename and filename in (d["module"], d["file_name"])):
            return d
    return None


def _artifact(name: str):
    """(bytes, content_type) of an uploaded artifact by exact name, via app.artifacts when §4 provides it."""
    try:
        from app import artifacts  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    for fn_name in ("read_artifact", "get_bytes", "read"):
        fn = getattr(artifacts, fn_name, None)
        if callable(fn):
            try:
                got = fn(name)
            except Exception as e:  # noqa: BLE001
                log.debug("artifact %s unreadable: %s", name, e)
                return None
            if got is None:
                return None
            if isinstance(got, tuple):
                return got
            return got, "application/octet-stream"
    return None


def _newest_artifact_name() -> tuple[str, str | None] | None:
    try:
        from app import artifacts  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    fn = getattr(artifacts, "newest", None)
    if not callable(fn):
        return None
    try:
        got = fn(HELPER_MODULE)
    except Exception:  # noqa: BLE001
        return None
    if not got:
        return None
    if isinstance(got, dict):
        return got.get("name"), got.get("version")
    return str(got), None


def _bundled_wheels() -> list[Path]:
    files = [Path(p) for p in glob.glob(str(DOWNLOADS_DIR / "kh_helper-*.whl"))]

    def ver(p: Path):
        m = _WHEEL_RE.match(p.name)
        parts = re.split(r"[.+-]", m.group(1)) if m else []
        return [int(x) if x.isdigit() else -1 for x in parts]

    return sorted(files, key=ver, reverse=True)


def helper_link(base: str) -> tuple[str | None, str | None]:
    """(helper url, helper version) advertised by /auth/config and used as {source} in the guide."""
    src = _db_source(module=HELPER_MODULE)
    if src:
        if src.get("type") == "https":
            return f"{base}/auth/download/{HELPER_MODULE}", src.get("version")
        name = src.get("file_name") or os.path.basename(src.get("path") or "") or HELPER_MODULE
        return f"{base}/auth/download/{name}", src.get("version")
    art = _newest_artifact_name()
    if art and art[0]:
        return f"{base}/auth/download/{art[0]}", art[1]
    wheels = _bundled_wheels()
    if wheels:
        m = _WHEEL_RE.match(wheels[0].name)
        return f"{base}/auth/download/{wheels[0].name}", (m.group(1) if m else None)
    return None, None


def _drive_direct(url: str) -> str:
    m = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?id=)([A-Za-z0-9_-]+)", url or "")
    if m:
        return f"https://drive.usercontent.google.com/download?id={m.group(1)}&export=download&confirm=t"
    return url


def _https_answer(src: dict) -> dict:
    url = src.get("path") or ""
    direct = _drive_direct(url)
    fname = src.get("file_name") or "kh-helper.zip"
    steps = [
        f"Download: curl -L -o {fname} \"{direct}\" (PowerShell: Invoke-WebRequest -Uri \"{direct}\" -OutFile {fname})",
        f"Check that the file starts with the bytes PK (a zip or wheel), not an HTML page.",
    ]
    if src.get("sha256"):
        steps.append(f"Check its sha256 equals {src['sha256']}.")
    steps.append("Install the wheel inside it into the helper venv: <venv>/python -m pip install --upgrade <wheel>, "
                 "then run <venv>/python -m kh_helper.cli setup.")
    return {"module": src.get("module"), "type": "https", "version": src.get("version"), "url": url,
            "direct_url": direct, "sha256": src.get("sha256"), "file_name": src.get("file_name"), "steps": steps,
            "updated_at": src.get("updated_at")}


def download(filename: str):
    src = _db_source(filename=filename)
    if src:
        if src.get("type") == "https":
            return JSONResponse(_https_answer(src))
        root = Path(config.ROOT).resolve()
        p = (root / (src.get("path") or "")).resolve()
        if root not in p.parents and p != root:
            raise HTTPException(status_code=404, detail="no such download")
        if p.is_file():
            return FileResponse(str(p), filename=src.get("file_name") or p.name)
    art = _artifact(filename)
    if art is not None:
        data, ctype = art
        return Response(content=data, media_type=ctype or "application/octet-stream",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    wheels = _bundled_wheels()
    pick = None
    if filename == HELPER_MODULE and wheels:
        pick = wheels[0]
    else:
        pick = next((w for w in wheels if w.name == filename), None)
    if pick is not None:
        return FileResponse(str(pick), filename=pick.name)
    raise HTTPException(status_code=404, detail="no such download")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class HelpBody(BaseModel):
    text: Optional[str] = None


def me_answer(request: Request | None = None) -> dict:
    p = auth.current()
    if p is None:
        raise HTTPException(status_code=404, detail="sign-in is off on this KH")
    if p.ns:
        raise HTTPException(status_code=403, detail="service tokens may call /v1 routes only")
    enc = None
    model = getattr(request.app.state, "model", None) if request is not None else None
    if model is not None:
        enc = lambda texts: model.encode(texts, normalize_embeddings=True)  # noqa: E731
    try:
        personal.touch(p.email, enc)
    except Exception as e:  # noqa: BLE001
        log.warning("personal project of %s not ensured: %s", p.email, e)
    return {"email": p.email, "personal_project": p.personal_project, "role": p.role,
            "permissions": sorted(p.kh_permissions), "all_projects": sorted(p.every_project),
            "projects": {pid: sorted(perms) for pid, perms in sorted(p.projects.items())}}


def attach(app: FastAPI) -> None:
    @app.get("/auth/help", tags=["auth"], response_class=PlainTextResponse)
    def auth_help(request: Request):
        text = guide_text()
        if text is None:
            text = SEED_PATH.read_text(encoding="utf-8") if SEED_PATH.exists() else \
                "Knowledge Hub sign-in guide is not available."
        base = _base_url(request)
        source, _ver = helper_link(base)
        text = (text.replace("{source}", source or f"{base}/auth/download/{HELPER_MODULE}")
                .replace("{kh_url}", base).replace("{issuer}", auth.issuer() or ""))
        return PlainTextResponse(text)

    @app.put("/auth/help", tags=["auth"])
    def auth_help_put(body: HelpBody, request: Request):
        if not body.text or not body.text.strip():
            raise HTTPException(status_code=422, detail="text is required")
        model = getattr(request.app.state, "model", None)
        if model is None:
            from app.embeddings import get_embedder

            model = get_embedder()
        _store_guide(body.text, lambda t: model.encode(t, normalize_embeddings=True))
        return {"doc": GUIDE_DOC, "id": GUIDE_ID, "chars": len(body.text)}

    @app.get("/auth/download/{filename}", tags=["auth"])
    def auth_download(filename: str):
        return download(filename)

    @app.get("/auth/config", tags=["auth"])
    def auth_config(request: Request):
        helper, ver = helper_link(_base_url(request))
        return {"issuer": auth.issuer(), "client_id": config.AUTH_CLIENT_ID or "kh-cli",
                "audience": auth.audience(), "help": auth.HELP, "helper": helper, "helper_version": ver}

    @app.get("/auth/me", tags=["auth"])
    def auth_me(request: Request):
        return me_answer(request)
