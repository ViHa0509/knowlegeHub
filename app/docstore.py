"""JSON document store (spec §1.5, table `kh_docs`).

Holds what were JSON files locally (`projects/<id>/counter.json`, `projects/<id>/bundle.json`,
`orch_manifest.json`). With `KH_STORE_BACKEND=hana` the documents live in `kh_docs` keyed by the path
relative to the app root (forward slashes); otherwise they are real files (temp file + os.replace).
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from app import config, sqldb

_table_ready = False


def _use_db() -> bool:
    return sqldb.is_hana()


def key_of(path: str | os.PathLike) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = config.ROOT / p
    try:
        rel = p.resolve().relative_to(config.ROOT.resolve())
        return rel.as_posix()
    except ValueError:
        return p.resolve().as_posix()


def _abs(path: str | os.PathLike) -> Path:
    p = Path(path)
    return p if p.is_absolute() else config.ROOT / p


def _ensure(c) -> None:
    global _table_ready
    if _table_ready:
        return
    t = config.DOCS_TABLE
    sqldb.create_table(
        c, t,
        f"CREATE TABLE IF NOT EXISTS {t} (path TEXT PRIMARY KEY, body TEXT, updated_at TEXT)",
        f"CREATE COLUMN TABLE {t} (path NVARCHAR(1024) PRIMARY KEY, body NCLOB, updated_at NVARCHAR(32))",
    )
    _table_ready = True


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_json(path, default: Any = None) -> Any:
    if _use_db():
        with sqldb.connect() as c:
            _ensure(c)
            r = c.execute(f"SELECT body FROM {config.DOCS_TABLE} WHERE path=?", (key_of(path),)).fetchone()
        if r is None or r[0] is None:
            return default
        try:
            return json.loads(r[0])
        except ValueError:
            return default
    p = _abs(path)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, obj: Any) -> None:
    body = json.dumps(obj, indent=2, ensure_ascii=False)
    if _use_db():
        with sqldb.connect() as c:
            _ensure(c)
            sqldb.upsert(c, config.DOCS_TABLE, ["path"], {"path": key_of(path), "body": body, "updated_at": _now()})
        return
    p = _abs(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def exists(path) -> bool:
    if _use_db():
        with sqldb.connect() as c:
            _ensure(c)
            return c.execute(f"SELECT 1 FROM {config.DOCS_TABLE} WHERE path=?", (key_of(path),)).fetchone() is not None
    return _abs(path).exists()


def delete(path) -> None:
    if _use_db():
        with sqldb.connect() as c:
            _ensure(c)
            c.execute(f"DELETE FROM {config.DOCS_TABLE} WHERE path=?", (key_of(path),))
        return
    try:
        _abs(path).unlink()
    except OSError:
        pass


def delete_tree(directory) -> int:
    """Delete every document under `directory`. Returns rows/files removed (best effort)."""
    if _use_db():
        prefix = key_of(directory).rstrip("/")
        esc = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with sqldb.connect() as c:
            _ensure(c)
            cur = c.execute(f"DELETE FROM {config.DOCS_TABLE} WHERE path LIKE ? ESCAPE '\\'", (esc + "/%",))
            return int(cur.rowcount or 0)
    p = _abs(directory)
    if not p.exists():
        return 0
    n = sum(1 for _ in p.rglob("*") if _.is_file())
    shutil.rmtree(p, ignore_errors=True)
    return n
