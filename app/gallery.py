"""Gallery (spec §4.12): designs listed by the public Designer site.

Tables `kh_gallery_design(design_no PK (2-3 digits), name, kind style|layer|motion, description, demo_label, alias,
tags, spec_id, ref_id, added, updated_at, updated_by, demo BLOB, thumb BLOB (WebP))` and
`kh_gallery_variant(design_no, idx, label, demo BLOB)`. The old table `kh_design_gallery` is dropped at startup.

Routes: `GET /gallery/designs?thumbs=1` (public, CORS `*`, thumbnails as data URIs when asked),
`GET /gallery/designs/{no}/thumb` (image/webp), `GET /gallery/designs/{no}/demo?v=` (HTML, sandbox CSP),
`PUT /gallery/designs/{no}` (project.write on `designer`), `DELETE /gallery/designs/{no}` (maintainer).
In-memory list cache `KH_GALLERY_CACHE_S` (300 s), dropped on write; size cap `KH_GALLERY_MAX_MB` (8).
On HANA, BLOBs are read through a raw cursor (sqldb would turn LOBs into text).
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from app import auth, config, sqldb

log = logging.getLogger(__name__)

KINDS = ("style", "layer", "motion")
DEMO_CSP = "sandbox allow-scripts allow-popups allow-forms allow-modals"
_NO = re.compile(r"^\d{2,3}$")
_META_COLS = ("design_no", "name", "kind", "description", "demo_label", "alias", "tags", "spec_id", "ref_id",
              "added", "updated_at", "updated_by")


def cache_seconds() -> float:
    return config._float("KH_GALLERY_CACHE_S", 300)


def max_bytes() -> int:
    return int(config._float("KH_GALLERY_MAX_MB", 8) * 1024 * 1024)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _m1(c):
    sqldb.create_table(
        c, "kh_gallery_design",
        "CREATE TABLE IF NOT EXISTS kh_gallery_design (design_no TEXT PRIMARY KEY, name TEXT NOT NULL, "
        "kind TEXT NOT NULL, description TEXT, demo_label TEXT, alias TEXT, tags TEXT, spec_id TEXT, ref_id TEXT, "
        "added TEXT, updated_at TEXT, updated_by TEXT, demo BLOB, thumb BLOB)",
        "CREATE COLUMN TABLE kh_gallery_design (design_no NVARCHAR(3) PRIMARY KEY, name NVARCHAR(256) NOT NULL, "
        "kind NVARCHAR(16) NOT NULL, description NVARCHAR(5000), demo_label NVARCHAR(256), alias NVARCHAR(256), "
        "tags NVARCHAR(2000), spec_id NVARCHAR(256), ref_id NVARCHAR(256), added NVARCHAR(40), "
        "updated_at NVARCHAR(40), updated_by NVARCHAR(256), demo BLOB, thumb BLOB)")
    sqldb.create_table(
        c, "kh_gallery_variant",
        "CREATE TABLE IF NOT EXISTS kh_gallery_variant (design_no TEXT NOT NULL, idx INTEGER NOT NULL, label TEXT, "
        "demo BLOB, PRIMARY KEY (design_no, idx))",
        "CREATE COLUMN TABLE kh_gallery_variant (design_no NVARCHAR(3) NOT NULL, idx INTEGER NOT NULL, "
        "label NVARCHAR(256), demo BLOB, PRIMARY KEY (design_no, idx))")


_ready: set = set()


def _drop_old(c) -> None:
    try:
        if sqldb.table_exists(c, "kh_design_gallery"):
            c.execute("DROP TABLE kh_design_gallery")
            log.info("dropped old table kh_design_gallery")
    except Exception as e:  # noqa: BLE001
        log.warning("could not drop kh_design_gallery: %s", e)


def _conn():
    c = sqldb.connect()
    k = sqldb._location(c)
    if k not in _ready:
        sqldb.migrate(c, "gallery", [(1, _m1)])
        c.commit()
        _ready.add(k)
    return c


def init() -> None:
    with _conn() as c:
        _drop_old(c)


def _blob_rows(sql: str, params: tuple) -> list[tuple]:
    """Rows with BLOB columns as bytes (HANA: raw cursor, LOB.read())."""
    with _conn() as c:
        if sqldb.is_hana():
            cur = c.raw.cursor()
            try:
                cur.execute(sql, params)
                rows = cur.fetchall()
            finally:
                cur.close()
        else:
            rows = [tuple(r) for r in c.execute(sql, params).fetchall()]
    out = []
    for r in rows:
        vals = []
        for v in r:
            read = getattr(v, "read", None)
            if callable(read):
                v = read()
            if isinstance(v, memoryview):
                v = bytes(v)
            vals.append(v)
        out.append(tuple(vals))
    return out


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache: dict[bool, tuple[float, list]] = {}
_cache_lock = threading.Lock()


def drop_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _tags(v) -> list:
    if not v:
        return []
    try:
        got = json.loads(v)
        return got if isinstance(got, list) else [str(got)]
    except (TypeError, ValueError):
        return [t.strip() for t in str(v).split(",") if t.strip()]


def list_designs(thumbs: bool = False) -> list[dict]:
    ttl = cache_seconds()
    with _cache_lock:
        hit = _cache.get(thumbs)
        if hit and ttl > 0 and time.monotonic() - hit[0] < ttl:
            return hit[1]
    cols = ", ".join(_META_COLS) + (", thumb" if thumbs else "")
    rows = _blob_rows(f"SELECT {cols} FROM kh_gallery_design ORDER BY design_no", ())
    variants: dict[str, list] = {}
    for no, idx, label in _blob_rows("SELECT design_no, idx, label FROM kh_gallery_variant ORDER BY design_no, idx",
                                     ()):
        variants.setdefault(no, []).append({"idx": int(idx), "label": label or ""})
    out = []
    for r in rows:
        d = dict(zip(_META_COLS, r[:len(_META_COLS)]))
        item = {"no": d["design_no"], "name": d["name"], "kind": d["kind"], "desc": d["description"] or "",
                "demo": d["demo_label"] or "", "alias": d["alias"] or "", "tags": _tags(d["tags"]),
                "spec": d["spec_id"] or "", "ref": d["ref_id"] or "", "added": d["added"] or "",
                "updated_at": d["updated_at"], "updated_by": d["updated_by"],
                "variants": variants.get(d["design_no"], [])}
        if thumbs:
            t = r[len(_META_COLS)]
            item["thumb"] = ("data:image/webp;base64," + base64.b64encode(t).decode("ascii")) if t else None
        out.append(item)
    with _cache_lock:
        _cache[thumbs] = (time.monotonic(), out)
    return out


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


class Variant(BaseModel):
    label: Optional[str] = None
    demo_b64: str


class DesignRequest(BaseModel):
    name: str
    kind: str
    desc: Optional[str] = None
    demo: Optional[str] = None
    alias: Optional[str] = None
    tags: Optional[Any] = None
    spec: Optional[str] = None
    ref: Optional[str] = None
    added: Optional[str] = None
    demo_b64: str
    thumb_b64: str
    variants: Optional[list[Variant]] = None


def _b64(field: str, s: str) -> bytes:
    s = (s or "").strip()
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail=f"{field} is not valid base64")


def _check_no(no: str) -> str:
    if not _NO.match(no or ""):
        raise HTTPException(status_code=400, detail="design number must be 2-3 digits")
    return no


def put_design(no: str, body: DesignRequest) -> dict:
    _check_no(no)
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    kind = (body.kind or "").strip().lower()
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail="kind must be one of style, layer, motion")
    demo = _b64("demo_b64", body.demo_b64)
    thumb = _b64("thumb_b64", body.thumb_b64)
    if not demo:
        raise HTTPException(status_code=400, detail="demo_b64 is required")
    variants = [(i, (v.label or "").strip(), _b64(f"variants[{i}].demo_b64", v.demo_b64))
                for i, v in enumerate(body.variants or [])]
    total = len(demo) + len(thumb) + sum(len(v[2]) for v in variants)
    limit = max_bytes()
    if total > limit:
        raise HTTPException(status_code=413,
                            detail=f"design is {total} bytes; the limit is {config._float('KH_GALLERY_MAX_MB', 8):g} MB")
    tags = body.tags
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    tags_json = json.dumps(list(tags or []), ensure_ascii=False)
    p = auth.current()
    now = _now()
    row = {"design_no": no, "name": name, "kind": kind, "description": body.desc or "",
           "demo_label": body.demo or "", "alias": body.alias or "", "tags": tags_json, "spec_id": body.spec or "",
           "ref_id": body.ref or "", "added": body.added or now[:10], "updated_at": now,
           "updated_by": getattr(p, "email", None), "demo": demo, "thumb": thumb}
    with _conn() as c:
        old = c.execute("SELECT added FROM kh_gallery_design WHERE design_no=?", (no,)).fetchone()
        if old is not None and not body.added and old["added"]:
            row["added"] = old["added"]
        sqldb.upsert(c, "kh_gallery_design", ["design_no"], row)
        c.execute("DELETE FROM kh_gallery_variant WHERE design_no=?", (no,))
        for idx, label, data in variants:
            c.execute("INSERT INTO kh_gallery_variant (design_no, idx, label, demo) VALUES (?, ?, ?, ?)",
                      (no, idx, label, data))
    drop_cache()
    return {"status": "ok", "no": no, "name": name, "kind": kind, "variants": len(variants), "bytes": total,
            "created": old is None}


def delete_design(no: str) -> bool:
    with _conn() as c:
        n = c.execute("DELETE FROM kh_gallery_design WHERE design_no=?", (no,)).rowcount
        c.execute("DELETE FROM kh_gallery_variant WHERE design_no=?", (no,))
    drop_cache()
    return bool(n and n > 0)


def _not_found(no: str):
    return HTTPException(status_code=404, detail=f"no design {no}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def attach(app: FastAPI) -> None:
    try:
        init()
    except Exception as e:  # noqa: BLE001
        log.warning("gallery tables not ready: %s", e)

    @app.get("/gallery/designs", tags=["gallery"])
    def gallery_designs(thumbs: int = 0):
        items = list_designs(bool(thumbs))
        return JSONResponse({"count": len(items), "designs": items},
                            headers={"Access-Control-Allow-Origin": "*"})

    @app.get("/gallery/designs/{no}/thumb", tags=["gallery"])
    def gallery_thumb(no: str):
        rows = _blob_rows("SELECT thumb FROM kh_gallery_design WHERE design_no=?", (no,))
        if not rows or not rows[0][0]:
            raise _not_found(no)
        return Response(content=rows[0][0], media_type="image/webp",
                        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=300"})

    @app.get("/gallery/designs/{no}/demo", tags=["gallery"])
    def gallery_demo(no: str, v: Optional[int] = None):
        if v is None:
            rows = _blob_rows("SELECT demo FROM kh_gallery_design WHERE design_no=?", (no,))
        else:
            rows = _blob_rows("SELECT demo FROM kh_gallery_variant WHERE design_no=? AND idx=?", (no, int(v)))
        if not rows or not rows[0][0]:
            if v is not None:
                raise HTTPException(status_code=404, detail=f"design {no} has no variant {v}")
            raise _not_found(no)
        return Response(content=rows[0][0], media_type="text/html; charset=utf-8",
                        headers={"Content-Security-Policy": DEMO_CSP})

    @app.put("/gallery/designs/{no}", tags=["gallery"])
    def gallery_put(no: str, body: DesignRequest):
        return put_design(no, body)

    @app.delete("/gallery/designs/{no}", tags=["gallery"])
    def gallery_delete(no: str):
        if not delete_design(no):
            raise _not_found(no)
        return {"deleted": no}
