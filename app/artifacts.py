"""Artifacts (spec §4.12): files KH hands out (the kh-helper wheel), so a helper release needs no redeploy.

Tables `artifact_file`, `artifact_blob` (base64), `artifact_settings` (active mode + config). Backends `hana`
(default; the relational store, also on SQLite), `http` (WebDAV-like PUT/GET/DELETE `{base_url, get_url?, headers?,
serve?}`), `s3` (`{bucket, region?, endpoint_url?, prefix?, access_key_id, secret_access_key, serve?}`; needs boto3,
imported lazily). `serve` = `proxy` (default) or `redirect`. Switching probes write/read/delete, copies every
artifact checked by sha256, then flips setting + rows in one transaction; old copies remain. Secrets masked on read.

Helpers for /auth/download (app/auth_routes.py): `read_artifact(name) -> (bytes, content_type) | None`,
`newest(module) -> {name, version} | None`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app import config, sqldb

log = logging.getLogger(__name__)

BACKENDS = ("hana", "http", "s3")
SECRET_KEYS = ("secret_access_key", "access_key_id", "headers", "password", "token")
_WHEEL = re.compile(r"^([A-Za-z0-9_.]+)-(\d[^-]*)-.*\.whl$")
HTTP = None  # test seam: requests-like session for the http backend


def max_bytes() -> int:
    return int(config._float("KH_ARTIFACT_MAX_MB", 50) * 1024 * 1024)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _m1(c):
    sqldb.create_table(
        c, "artifact_file",
        "CREATE TABLE IF NOT EXISTS artifact_file (name TEXT PRIMARY KEY, module TEXT, version TEXT, sha256 TEXT, "
        "size INTEGER, content_type TEXT, backend TEXT, created_at TEXT, created_by TEXT)",
        "CREATE COLUMN TABLE artifact_file (name NVARCHAR(256) PRIMARY KEY, module NVARCHAR(128), "
        "version NVARCHAR(64), sha256 NVARCHAR(64), size BIGINT, content_type NVARCHAR(128), backend NVARCHAR(16), "
        "created_at NVARCHAR(40), created_by NVARCHAR(256))")
    sqldb.create_table(
        c, "artifact_blob",
        "CREATE TABLE IF NOT EXISTS artifact_blob (name TEXT PRIMARY KEY, content_b64 TEXT)",
        "CREATE COLUMN TABLE artifact_blob (name NVARCHAR(256) PRIMARY KEY, content_b64 NCLOB)")
    sqldb.create_table(
        c, "artifact_settings",
        "CREATE TABLE IF NOT EXISTS artifact_settings (id INTEGER PRIMARY KEY, mode TEXT, config TEXT, updated_at TEXT)",
        "CREATE COLUMN TABLE artifact_settings (id INTEGER PRIMARY KEY, mode NVARCHAR(16), config NCLOB, "
        "updated_at NVARCHAR(40))")


_ready: set = set()


def _conn():
    c = sqldb.connect()
    k = sqldb._location(c)
    if k not in _ready:
        sqldb.migrate(c, "artifacts", [(1, _m1)])
        c.commit()
        _ready.add(k)
    return c


def settings() -> dict:
    with _conn() as c:
        r = c.execute("SELECT mode, config FROM artifact_settings WHERE id=1").fetchone()
    if r is None:
        return {"mode": "hana", "config": {}}
    return {"mode": r["mode"] or "hana", "config": json.loads(r["config"] or "{}")}


def masked(cfg: dict) -> dict:
    return {k: ("***" if k in SECRET_KEYS and v else v) for k, v in (cfg or {}).items()}


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class _Hana:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    def put(self, name: str, data: bytes, c=None):
        row = {"name": name, "content_b64": base64.b64encode(data).decode()}
        if c is not None:
            sqldb.upsert(c, "artifact_blob", ["name"], row)
        else:
            with _conn() as cc:
                sqldb.upsert(cc, "artifact_blob", ["name"], row)

    def get(self, name: str) -> bytes | None:
        with _conn() as c:
            r = c.execute("SELECT content_b64 FROM artifact_blob WHERE name=?", (name,)).fetchone()
        return base64.b64decode(r[0]) if r and r[0] else None

    def delete(self, name: str):
        with _conn() as c:
            c.execute("DELETE FROM artifact_blob WHERE name=?", (name,))


class _Http:
    def __init__(self, cfg: dict):
        if not cfg.get("base_url"):
            raise HTTPException(status_code=422, detail="http backend needs base_url")
        self.cfg = cfg
        self.s = HTTP
        if self.s is None:
            import requests

            self.s = requests

    def _url(self, name: str) -> str:
        return self.cfg["base_url"].rstrip("/") + "/" + name

    def put(self, name: str, data: bytes, c=None):  # noqa: ARG002
        r = self.s.put(self._url(name), data=data, headers=self.cfg.get("headers") or {}, timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"PUT answered {r.status_code}")

    def get(self, name: str) -> bytes | None:
        url = (self.cfg.get("get_url") or self.cfg["base_url"]).rstrip("/") + "/" + name
        r = self.s.get(url, headers=self.cfg.get("headers") or {}, timeout=60)
        return r.content if r.status_code < 300 else None

    def delete(self, name: str):
        self.s.delete(self._url(name), headers=self.cfg.get("headers") or {}, timeout=60)


class _S3:
    def __init__(self, cfg: dict):
        try:
            import boto3  # type: ignore  # lazy optional dependency
        except ImportError:
            raise HTTPException(status_code=501, detail="s3 backend needs boto3 installed")
        if not cfg.get("bucket"):
            raise HTTPException(status_code=422, detail="s3 backend needs bucket")
        self.cfg = cfg
        self.client = boto3.client("s3", region_name=cfg.get("region"), endpoint_url=cfg.get("endpoint_url"),
                                   aws_access_key_id=cfg.get("access_key_id"),
                                   aws_secret_access_key=cfg.get("secret_access_key"))

    def _key(self, name):
        return (self.cfg.get("prefix") or "") + name

    def put(self, name, data, c=None):  # noqa: ARG002
        self.client.put_object(Bucket=self.cfg["bucket"], Key=self._key(name), Body=data)

    def get(self, name):
        try:
            return self.client.get_object(Bucket=self.cfg["bucket"], Key=self._key(name))["Body"].read()
        except Exception:  # noqa: BLE001
            return None

    def delete(self, name):
        self.client.delete_object(Bucket=self.cfg["bucket"], Key=self._key(name))

    def presign(self, name):
        return self.client.generate_presigned_url("get_object", Params={"Bucket": self.cfg["bucket"],
                                                                        "Key": self._key(name)}, ExpiresIn=600)


def backend(mode: str | None = None, cfg: dict | None = None):
    s = settings() if mode is None else {"mode": mode, "config": cfg or {}}
    return {"hana": _Hana, "http": _Http, "s3": _S3}[s["mode"]](s["config"])


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def _ctype(name: str) -> str:
    if name.endswith(".whl") or name.endswith(".zip"):
        return "application/zip"
    if name.endswith(".json"):
        return "application/json"
    if name.endswith(".txt") or name.endswith(".md"):
        return "text/plain"
    return "application/octet-stream"


def upload(name: str, content_b64: str, module: str | None = None, version: str | None = None,
           by: str | None = None) -> dict:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=422, detail="name must be a plain file name")
    try:
        data = base64.b64decode(content_b64, validate=True)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="content_b64 is not valid base64")
    if len(data) > max_bytes():
        raise HTTPException(status_code=413, detail=f"artifact larger than {config._float('KH_ARTIFACT_MAX_MB', 50)} MB")
    m = _WHEEL.match(name)
    if m:
        module = module or m.group(1)
        version = version or m.group(2)
    s = settings()
    b = backend()
    row = {"name": name, "module": module, "version": version, "sha256": hashlib.sha256(data).hexdigest(),
           "size": len(data), "content_type": _ctype(name), "backend": s["mode"], "created_at": _now(),
           "created_by": by or "local"}
    with _conn() as c:
        if s["mode"] == "hana":
            b.put(name, data, c)
        else:
            b.put(name, data)
        sqldb.upsert(c, "artifact_file", ["name"], row)
    return row


def list_artifacts(module: Optional[str] = None) -> list[dict]:
    with _conn() as c:
        if module:
            rows = c.execute("SELECT * FROM artifact_file WHERE module=? ORDER BY created_at DESC", (module,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM artifact_file ORDER BY created_at DESC").fetchall()
    return [{k: r[k] for k in ("name", "module", "version", "sha256", "size", "content_type", "backend",
                               "created_at", "created_by")} for r in rows]


def _row(name: str):
    with _conn() as c:
        return c.execute("SELECT * FROM artifact_file WHERE name=?", (name,)).fetchone()


def read_artifact(name: str):
    """(bytes, content_type) of an uploaded artifact by exact name, or None."""
    r = _row(name)
    if r is None:
        return None
    data = backend(r["backend"], settings()["config"] if r["backend"] == settings()["mode"] else {}).get(name) \
        if r["backend"] != "hana" else _Hana({}).get(name)
    if data is None:
        return None
    return data, r["content_type"] or "application/octet-stream"


def _vkey(v: str | None):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-+]", v or "0"))


def newest(module: str) -> dict | None:
    norm = module.replace("-", "_")
    cands = [a for a in list_artifacts() if (a["module"] or "").replace("-", "_") == norm]
    if not cands:
        return None
    best = max(cands, key=lambda a: _vkey(a["version"]))
    return {"name": best["name"], "version": best["version"]}


def delete(name: str) -> bool:
    r = _row(name)
    if r is None:
        return False
    try:
        if r["backend"] == "hana":
            _Hana({}).delete(name)
        else:
            backend().delete(name)
    except Exception as e:  # noqa: BLE001
        log.warning("artifact copy delete failed: %s", e)
    with _conn() as c:
        c.execute("DELETE FROM artifact_file WHERE name=?", (name,))
    return True


def switch(mode: str, cfg: dict) -> dict:
    if mode not in BACKENDS:
        raise HTTPException(status_code=422, detail=f"mode must be one of {', '.join(BACKENDS)}")
    if cfg.get("serve") not in (None, "proxy", "redirect"):
        raise HTTPException(status_code=422, detail="serve must be proxy or redirect")
    new = backend(mode, cfg)
    probe = b"kh artifact probe"
    pname = "__kh_probe__"
    try:
        new.put(pname, probe)
        if new.get(pname) != probe:
            raise RuntimeError("read back differs")
        new.delete(pname)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"backend probe failed: {e}")
    copied = []
    for a in list_artifacts():
        got = read_artifact(a["name"])
        if got is None:
            continue
        new.put(a["name"], got[0])
        back = new.get(a["name"])
        if back is None or hashlib.sha256(back).hexdigest() != a["sha256"]:
            raise HTTPException(status_code=502, detail=f"copy of {a['name']} failed the sha256 check")
        copied.append(a["name"])
    with _conn() as c:
        sqldb.upsert(c, "artifact_settings", ["id"], {"id": 1, "mode": mode, "config": json.dumps(cfg),
                                                      "updated_at": _now()})
        for n in copied:
            c.execute("UPDATE artifact_file SET backend=? WHERE name=?", (mode, n))
    return {"mode": mode, "config": masked(cfg), "copied": copied}


class UploadRequest(BaseModel):
    name: str
    content_b64: str
    module: Optional[str] = None
    version: Optional[str] = None


class BackendRequest(BaseModel):
    mode: str
    config: dict = Field(default_factory=dict)


def attach(app: FastAPI) -> None:
    @app.post("/artifacts", tags=["artifacts"])
    def artifacts_upload(body: UploadRequest):
        from app import auth

        p = auth.current()
        return upload(body.name, body.content_b64, body.module, body.version, getattr(p, "email", None))

    @app.get("/artifacts", tags=["artifacts"])
    def artifacts_list(module: Optional[str] = None):
        return {"artifacts": list_artifacts(module)}

    @app.delete("/artifacts/{name}", tags=["artifacts"])
    def artifacts_delete(name: str):
        if not delete(name):
            raise HTTPException(status_code=404, detail="no such artifact")
        return {"deleted": name}

    @app.get("/artifacts/backend", tags=["artifacts"])
    def artifacts_backend():
        s = settings()
        return {"mode": s["mode"], "config": masked(s["config"])}

    @app.put("/artifacts/backend", tags=["artifacts"])
    def artifacts_backend_put(body: BackendRequest):
        return switch(body.mode, body.config)
