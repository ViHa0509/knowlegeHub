"""Download sources (spec §4.12): where `GET /auth/download/{filename}` finds a module's file.

Table `kh_download_source(module PK, type local|https, path, version, sha256, file_name, notes, updated_at,
updated_by)`. `local` = a file under the app folder (the path may not escape it); `https` = a public link that
`/auth/download` answers as JSON install steps. Routes (maintainer): `GET /downloads`,
`PUT /downloads/{module} {type, path, version?, sha256?, file_name?, notes?}`, `DELETE /downloads/{module}`.
`/auth/download` and `/auth/config` (app/auth_routes.py) read the table directly.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import auth, config, sqldb

log = logging.getLogger(__name__)

TYPES = ("local", "https")
COLUMNS = ("module", "type", "path", "version", "sha256", "file_name", "notes", "updated_at", "updated_by")
_MODULE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
_SHA = re.compile(r"^[0-9a-fA-F]{64}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _m1(c):
    sqldb.create_table(
        c, "kh_download_source",
        "CREATE TABLE IF NOT EXISTS kh_download_source (module TEXT PRIMARY KEY, type TEXT NOT NULL, "
        "path TEXT NOT NULL, version TEXT, sha256 TEXT, file_name TEXT, notes TEXT, updated_at TEXT, "
        "updated_by TEXT)",
        "CREATE COLUMN TABLE kh_download_source (module NVARCHAR(128) PRIMARY KEY, type NVARCHAR(16) NOT NULL, "
        "path NVARCHAR(2000) NOT NULL, version NVARCHAR(64), sha256 NVARCHAR(64), file_name NVARCHAR(256), "
        "notes NVARCHAR(2000), updated_at NVARCHAR(40), updated_by NVARCHAR(256))")


_ready: set = set()


def _conn():
    c = sqldb.connect()
    k = sqldb._location(c)
    if k not in _ready:
        sqldb.migrate(c, "downloads", [(1, _m1)])
        c.commit()
        _ready.add(k)
    return c


def init() -> None:
    with _conn():
        pass


class SourceRequest(BaseModel):
    type: str
    path: str
    version: Optional[str] = None
    sha256: Optional[str] = None
    file_name: Optional[str] = None
    notes: Optional[str] = None


def _row(r) -> dict:
    return {k: r[k] for k in COLUMNS}


def list_sources() -> list[dict]:
    with _conn() as c:
        rows = c.execute(f"SELECT {', '.join(COLUMNS)} FROM kh_download_source ORDER BY module").fetchall()
    return [_row(r) for r in rows]


def get_source(module: str) -> dict | None:
    with _conn() as c:
        r = c.execute(f"SELECT {', '.join(COLUMNS)} FROM kh_download_source WHERE module=?", (module,)).fetchone()
    return _row(r) if r is not None else None


def _validate(module: str, body: SourceRequest) -> tuple[str, str]:
    if not _MODULE.match(module or ""):
        raise HTTPException(status_code=400, detail="module must be 1-128 characters of letters, digits, '.', '_', '-'")
    typ = (body.type or "").strip().lower()
    if typ not in TYPES:
        raise HTTPException(status_code=400, detail="type must be one of local, https")
    path = (body.path or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    if typ == "https":
        if not path.lower().startswith("https://"):
            raise HTTPException(status_code=400, detail="an https source needs a path starting with https://")
    else:
        root = Path(config.ROOT).resolve()
        p = (root / path).resolve()
        if root not in p.parents:
            raise HTTPException(status_code=400, detail="a local path must stay inside the app folder")
    if body.sha256 and not _SHA.match(body.sha256.strip()):
        raise HTTPException(status_code=400, detail="sha256 must be 64 hex characters")
    return typ, path


def put_source(module: str, body: SourceRequest) -> dict:
    typ, path = _validate(module, body)
    p = auth.current()
    row = {"module": module, "type": typ, "path": path, "version": body.version,
           "sha256": body.sha256.strip().lower() if body.sha256 else None, "file_name": body.file_name,
           "notes": body.notes, "updated_at": _now(), "updated_by": getattr(p, "email", None)}
    with _conn() as c:
        sqldb.upsert(c, "kh_download_source", ["module"], row)
    return row


def delete_source(module: str) -> bool:
    with _conn() as c:
        n = c.execute("DELETE FROM kh_download_source WHERE module=?", (module,)).rowcount
    return bool(n and n > 0)


def attach(app: FastAPI) -> None:
    try:
        init()
    except Exception as e:  # noqa: BLE001
        log.warning("download sources table not ready: %s", e)

    @app.get("/downloads", tags=["downloads"])
    def downloads_list():
        items = list_sources()
        return {"count": len(items), "sources": items}

    @app.put("/downloads/{module}", tags=["downloads"])
    def downloads_put(module: str, body: SourceRequest):
        return {"status": "ok", "source": put_source(module, body)}

    @app.delete("/downloads/{module}", tags=["downloads"])
    def downloads_delete(module: str):
        if not delete_source(module):
            raise HTTPException(status_code=404, detail=f"no download source '{module}'")
        return {"deleted": module}
