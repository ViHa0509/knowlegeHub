"""Kernel tag vocabulary (spec §3.8.1): table kernel_tag, seeding, the lexical `suggest` ranker."""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Optional

from app import sqldb
from app import ctx_util as U

TAG_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
STATUSES = ("active", "proposed", "retired")
GENERIC_FACETS = ("moment", "cli")
CACHE_SECONDS = 30.0

_SQLITE = """CREATE TABLE IF NOT EXISTS kernel_tag (
    tag TEXT PRIMARY KEY, facet TEXT NOT NULL, description TEXT, aliases TEXT DEFAULT '[]',
    status TEXT DEFAULT 'active', added_by TEXT, added_at TEXT)"""
_HANA = """CREATE COLUMN TABLE kernel_tag (
    tag NVARCHAR(48) PRIMARY KEY, facet NVARCHAR(48) NOT NULL, description NVARCHAR(1000),
    aliases NVARCHAR(4000) DEFAULT '[]', status NVARCHAR(16) DEFAULT 'active', added_by NVARCHAR(320),
    added_at NVARCHAR(40))"""
_COLS = ("tag", "facet", "description", "aliases", "status", "added_by", "added_at")

_ready = False
_cache: dict = {"at": 0.0, "rows": None}
_lock = threading.Lock()

STOP_WORDS = frozenset("""
a an and are as at be but by for from has have i if in into is it its of on or so that the their them then there
these they this to was were will with you your we our not no do does did can could should would may might must
any all each every when what which who whom how why where than too very just also only about over under after
before up down out off own same such more most other some one two me my he she his her am been being let
""".split())
_TOKEN = re.compile(r"[a-z0-9_]+")
_SUFFIXES = ("ing", "ies", "es", "ed", "s")


def ensure() -> None:
    global _ready
    if _ready:
        return
    with sqldb.connect() as c:
        sqldb.create_table(c, "kernel_tag", _SQLITE, _HANA)
        sqldb.create_index(c, "ix_kernel_tag_facet", "kernel_tag", ["facet", "status"])
    _ready = True


def invalidate() -> None:
    with _lock:
        _cache["rows"] = None
        _cache["at"] = 0.0


def _row(r) -> dict:
    d = {k: r[i] for i, k in enumerate(_COLS)}
    d["aliases"] = U.jload(d["aliases"], [])
    return d


def all_rows() -> dict[str, dict]:
    """Every tag row (cached for 30 s per process)."""
    with _lock:
        if _cache["rows"] is not None and time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["rows"]
    ensure()
    with sqldb.connect() as c:
        rows = {r[0]: _row(r) for r in c.execute(f"SELECT {', '.join(_COLS)} FROM kernel_tag").fetchall()}
    with _lock:
        _cache["rows"] = rows
        _cache["at"] = time.time()
    return rows


def active() -> dict[str, dict]:
    return {t: r for t, r in all_rows().items() if r["status"] == "active"}


def facet_of(tag: str) -> Optional[str]:
    r = all_rows().get(tag)
    return r["facet"] if r else None


def list_tags(facet: Optional[str] = None, status: Optional[str] = "active") -> dict:
    rows = [r for r in all_rows().values() if (not facet or r["facet"] == facet) and (not status or r["status"] == status)]
    rows.sort(key=lambda r: (r["facet"], r["tag"]))
    facets: dict[str, int] = {}
    for r in rows:
        facets[r["facet"]] = facets.get(r["facet"], 0) + 1
    return {"count": len(rows), "facets": facets, "tags": rows}


def count() -> int:
    ensure()
    with sqldb.connect() as c:
        return int(c.execute("SELECT COUNT(*) FROM kernel_tag").fetchone()[0])


def seed(data: dict, added_by: str = "seed") -> dict:
    """Load `{facets: {<facet>: {description, tags: [[tag, desc, "a1,a2"], ...]}}}`. Existing status is kept."""
    ensure()
    seen: dict[str, str] = {}
    rejected: list[dict] = []
    rows: list[dict] = []
    for facet, fd in (data.get("facets") or {}).items():
        for item in fd.get("tags") or []:
            tag = str(item[0]).strip().lower()
            desc = item[1] if len(item) > 1 else ""
            aliases = [a.strip() for a in str(item[2] if len(item) > 2 else "").split(",") if a.strip()]
            if not TAG_RE.match(tag):
                rejected.append({"tag": tag, "why": "bad id"})
                continue
            if tag in seen and seen[tag] != facet:
                rejected.append({"tag": tag, "why": f"listed in two facets ({seen[tag]}, {facet})"})
                continue
            seen[tag] = facet
            rows.append({"tag": tag, "facet": facet, "description": desc, "aliases": json.dumps(aliases)})
    now = U.now_iso()
    added = updated = 0
    with sqldb.connect() as c:
        for r in rows:
            old = c.execute("SELECT status FROM kernel_tag WHERE tag=?", (r["tag"],)).fetchone()
            if old is None:
                sqldb.insert_new(c, "kernel_tag", {**r, "status": "active", "added_by": added_by, "added_at": now})
                added += 1
            else:
                c.execute("UPDATE kernel_tag SET facet=?, description=?, aliases=? WHERE tag=?",
                          (r["facet"], r["description"], r["aliases"], r["tag"]))
                updated += 1
    invalidate()
    return {"added": added, "updated": updated, "rejected": rejected, "facets": len(data.get("facets") or {})}


def propose(tag: str, facet: str, description: str, aliases, added_by: Optional[str]) -> dict:
    ensure()
    if isinstance(aliases, str):
        aliases = [a.strip() for a in aliases.split(",") if a.strip()]
    with sqldb.connect() as c:
        sqldb.insert_new(c, "kernel_tag", {"tag": tag, "facet": facet, "description": description,
                                           "aliases": json.dumps(list(aliases or [])), "status": "proposed",
                                           "added_by": added_by, "added_at": U.now_iso()})
    invalidate()
    return all_rows().get(tag) or {}


def set_status(tag: str, status: str) -> bool:
    ensure()
    with sqldb.connect() as c:
        n = c.execute("UPDATE kernel_tag SET status=? WHERE tag=?", (status, tag)).rowcount
    invalidate()
    return bool(n)


def facets() -> set[str]:
    return {r["facet"] for r in all_rows().values()}


# ---------------------------------------------------------------------------
# Lexical ranker
# ---------------------------------------------------------------------------


def stem(w: str) -> str:
    for suf in _SUFFIXES:
        if w.endswith(suf) and len(w) > len(suf) + 3:
            return w[:-3] + "y" if suf == "ies" else w[:-len(suf)]
    return w


def words(text: str | None) -> list[str]:
    """Stemmed [a-z0-9_]+ tokens, minus stop words, minus 1-char tokens (order kept, duplicates kept)."""
    out = []
    for t in _TOKEN.findall((text or "").lower()):
        if len(t) <= 1 or t in STOP_WORDS:
            continue
        out.append(stem(t))
    return out


def _stem_phrase(text: str) -> str:
    return " ".join(stem(t) for t in _TOKEN.findall(text.lower()))


def suggest(text: str, limit: int = 20, facet: Optional[str] = None, floor: float = 1.5) -> list[dict]:
    low = (text or "").lower()
    toks = set(words(text))
    stemmed = " " + _stem_phrase(text or "") + " "
    out = []
    for tag, r in active().items():
        if facet and r["facet"] != facet:
            continue
        score = 0.0
        why: list[str] = []
        for a in r["aliases"]:
            al = a.lower().strip()
            if not al:
                continue
            if " " in al or not al.replace("_", "").isalnum():
                sp = _stem_phrase(al)
                if al in low or (sp and " " + sp + " " in stemmed):
                    score += 3.0
                    why.append(f"phrase '{a}'")
            elif stem(al) in toks:
                score += 1.5
                why.append(f"word '{a}'")
        if r["facet"] != "kh":
            parts = [stem(p) for p in tag.split("_") if len(p) > 1 and p not in STOP_WORDS]
            if parts and all(p in toks for p in parts):
                score += 2.0
                why.append(f"tag '{tag}'")
        shared = toks & set(words(r.get("description")))
        if shared:
            score += 0.3 * len(shared)
            why.append("description: " + ", ".join(sorted(shared)[:3]))
        if score >= floor:
            out.append({"tag": tag, "facet": r["facet"], "score": round(min(score, 12.0), 2), "why": why[:4]})
    out.sort(key=lambda x: (-x["score"], x["tag"]))
    return out[:max(1, int(limit))]
