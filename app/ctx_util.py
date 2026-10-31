"""Small helpers shared by the §3 context-engine modules (rebuild-only module)."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app import auth
from app.projects import fold  # noqa: F401  (re-export: §3 "fold(x)")

_WS = re.compile(r"\s+")


def principal():
    try:
        return auth.current()
    except Exception:  # noqa: BLE001
        return None


def signed_in() -> bool:
    return principal() is not None


def is_maintainer() -> bool:
    """Maintainer, or signed out (local trial: everything allowed)."""
    p = principal()
    return p is None or bool(getattr(p, "is_maintainer", False))


def email() -> str | None:
    p = principal()
    return getattr(p, "email", None) if p is not None else None


def personal_tier() -> str | None:
    p = principal()
    if p is None or getattr(p, "ns", None):
        return None
    try:
        return p.personal_project
    except Exception:  # noqa: BLE001
        from app.personal import personal_id

        return personal_id(p.email)


def may(project: str | None, perm: str = "project.read") -> bool:
    try:
        return bool(auth.may(project, perm))
    except Exception:  # noqa: BLE001
        return False


def need(project: str | None, perm: str = "project.read") -> None:
    if project and not may(project, perm):
        raise HTTPException(status_code=403, detail=f"missing permission {perm} on this project")


def need_maintainer() -> None:
    if not is_maintainer():
        raise HTTPException(status_code=403, detail="maintainer only")


def readable_projects() -> list[str]:
    """Every listed project the caller may read."""
    from app import projects

    ids = sorted(projects.list_projects())
    vis = auth.visible_projects("project.read")
    if vis is None:
        return ids
    out = [i for i in ids if i in vis]
    for v in sorted(vis):
        if v not in out:
            out.append(v)
    return out


def collapse(text: str | None) -> str:
    return _WS.sub(" ", text or "").strip()


def short(text: str | None, limit: int) -> str:
    """Collapse whitespace; cut at the first '. ' or newline before the limit (keeping the period),
    else cut to limit-3 and add '...' (§3.4 `_short`)."""
    raw = (text or "").strip()
    cut_nl = raw.find("\n")
    flat = collapse(raw)
    cand = []
    i = flat.find(". ")
    if i >= 0:
        cand.append(i + 1)
    if cut_nl >= 0:
        j = collapse(raw[:cut_nl])
        cand.append(len(j))
    cand = [c for c in cand if 0 < c <= limit]
    if cand:
        return flat[:min(cand)].rstrip()
    if len(flat) <= limit:
        return flat
    return flat[:max(0, limit - 3)].rstrip() + "..."


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def jload(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return default


def model_of(request) -> Any:
    m = getattr(getattr(request, "app", None), "state", None)
    m = getattr(m, "model", None) if m is not None else None
    if m is None:
        from app.embeddings import get_embedder

        m = get_embedder()
    return m
