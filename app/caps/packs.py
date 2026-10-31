"""packs (spec §6.12): versioned document packs with stable citations, assumptions and duplicate questions.

KH hashes, freezes, cites and re-anchors text; it never judges meaning. Versions are labelled `vN.k` (N major from
counter `pkv:<ns>:<pack>`, k addendum from `pka:<ns>:<pack>:<N>`). Freezing chunks every part into quotes
(`path#page#offset`), supersedes the previous frozen version and re-anchors its quotes (map old -> new or NULL).
Frozen chunk quotes are embedded into `ns_<ns>_packs` (the `packs` tier of `/v1/{ns}/context/step`); questions into
`ns_<ns>_questions` for duplicate detection.
"""
from __future__ import annotations

import os
import re
import unicodedata
from typing import Any

from fastapi import Depends, FastAPI, Query, Request

from app import sqldb
from app.caps import core
from app.caps.core import CapError, Ctx

NAME = "packs"
VERSION = "1.0.0"
STATUS = "beta"
DEFAULT_ON = True

MAX_PARTS = 2000
PART_MAX_CHARS = 512 * 1024
VERSION_MAX_CHARS = 16 * 1024 * 1024
CHUNK_CHARS = 1200
QUOTE_MAX_CHARS = 20000
LIST_MAX = 1000
LIST_DEFAULT = 1000
MAP_HOPS_MAX = 1000
TEXT_MAX = 4000
QUESTION_MAX = 2000
DUP_THRESHOLD = 0.92
DUP_MIN, DUP_MAX = 0.80, 0.99
LOOKUP_K = 10
PATH_MAX = 400
PAGE_MAX = 1_000_000
TITLE_MAX = 400          # UNKNOWN in spec
NOTE_MAX = 2000          # UNKNOWN in spec
PACK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
LABEL_RE = re.compile(r"^v(\d+)(?:\.(\d+))?$")
ASM_STATUSES = ("open", "confirmed", "refuted", "superseded")
Q_STATUSES = ("open", "answered", "closed")
LIMITS = {"parts_per_version": MAX_PARTS, "part_chars": PART_MAX_CHARS, "version_chars": VERSION_MAX_CHARS,
          "chunk_chars": CHUNK_CHARS, "quote_chars": QUOTE_MAX_CHARS, "list_max": LIST_MAX,
          "quote_map_hops": MAP_HOPS_MAX, "text_chars": TEXT_MAX, "question_chars": QUESTION_MAX,
          "duplicate_threshold": DUP_THRESHOLD, "duplicate_threshold_range": [DUP_MIN, DUP_MAX],
          "lookup_k": LOOKUP_K}
TABLES = ["kh_pack_head", "kh_pack", "kh_pack_part", "kh_pack_blob", "kh_quote", "kh_quote_map", "kh_assumption",
          "kh_pack_question"]
LOCAL_TABLES: list[str] = []
VERSION_VERBS = ("freeze", "reindex")


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _t(c, name: str, cols_sqlite: str, cols_hana: str, pk: str) -> None:
    sqldb.create_table(c, name, f"CREATE TABLE IF NOT EXISTS {name} ({cols_sqlite}, PRIMARY KEY ({pk}))",
                       f"CREATE COLUMN TABLE {name} ({cols_hana}, PRIMARY KEY ({pk}))")


def _m1(c) -> None:
    _t(c, "kh_pack_head",
       "ns TEXT NOT NULL, pack_id TEXT NOT NULL, project TEXT, title TEXT, latest_ver INTEGER NOT NULL DEFAULT 0, "
       "latest_add INTEGER NOT NULL DEFAULT 0, actor_json TEXT, created_ms INTEGER, updated_ms INTEGER, "
       "rev INTEGER NOT NULL DEFAULT 1",
       "ns NVARCHAR(64) NOT NULL, pack_id NVARCHAR(64) NOT NULL, project NVARCHAR(256), title NVARCHAR(1000), "
       "latest_ver BIGINT NOT NULL, latest_add BIGINT NOT NULL, actor_json NCLOB, created_ms BIGINT, "
       "updated_ms BIGINT, rev BIGINT NOT NULL", "ns, pack_id")
    _t(c, "kh_pack",
       "ns TEXT NOT NULL, pack_id TEXT NOT NULL, version INTEGER NOT NULL, addendum INTEGER NOT NULL, label TEXT, "
       "sha256 TEXT, state TEXT NOT NULL, base_label TEXT, parts_n INTEGER, chars INTEGER, index_state TEXT, "
       "actor_json TEXT, created_ms INTEGER, frozen_ms INTEGER, rev INTEGER NOT NULL DEFAULT 1",
       "ns NVARCHAR(64) NOT NULL, pack_id NVARCHAR(64) NOT NULL, version BIGINT NOT NULL, addendum BIGINT NOT NULL, "
       "label NVARCHAR(32), sha256 NVARCHAR(64), state NVARCHAR(16) NOT NULL, base_label NVARCHAR(32), "
       "parts_n BIGINT, chars BIGINT, index_state NVARCHAR(16), actor_json NCLOB, created_ms BIGINT, "
       "frozen_ms BIGINT, rev BIGINT NOT NULL", "ns, pack_id, version, addendum")
    _t(c, "kh_pack_part",
       "ns TEXT NOT NULL, pack_id TEXT NOT NULL, version INTEGER NOT NULL, addendum INTEGER NOT NULL, "
       "path TEXT NOT NULL, page INTEGER NOT NULL, text_ref TEXT, sha256 TEXT, chars INTEGER",
       "ns NVARCHAR(64) NOT NULL, pack_id NVARCHAR(64) NOT NULL, version BIGINT NOT NULL, addendum BIGINT NOT NULL, "
       "path NVARCHAR(400) NOT NULL, page BIGINT NOT NULL, text_ref NVARCHAR(64), sha256 NVARCHAR(64), chars BIGINT",
       "ns, pack_id, version, addendum, path, page")
    _t(c, "kh_pack_blob", "ns TEXT NOT NULL, sha256 TEXT NOT NULL, text TEXT",
       "ns NVARCHAR(64) NOT NULL, sha256 NVARCHAR(64) NOT NULL, text NCLOB", "ns, sha256")
    _t(c, "kh_quote",
       "ns TEXT NOT NULL, pack_id TEXT NOT NULL, version TEXT NOT NULL, quote_id TEXT NOT NULL, path TEXT, "
       "page INTEGER, q_offset INTEGER, q_length INTEGER, text TEXT, sha256 TEXT, origin TEXT, created_ms INTEGER",
       "ns NVARCHAR(64) NOT NULL, pack_id NVARCHAR(64) NOT NULL, version NVARCHAR(32) NOT NULL, "
       "quote_id NVARCHAR(512) NOT NULL, path NVARCHAR(400), page BIGINT, q_offset BIGINT, q_length BIGINT, "
       "text NCLOB, sha256 NVARCHAR(64), origin NVARCHAR(8), created_ms BIGINT", "ns, pack_id, version, quote_id")
    _t(c, "kh_quote_map",
       "ns TEXT NOT NULL, pack_id TEXT NOT NULL, from_ver TEXT NOT NULL, old_quote_id TEXT NOT NULL, to_ver TEXT, "
       "new_quote_id TEXT",
       "ns NVARCHAR(64) NOT NULL, pack_id NVARCHAR(64) NOT NULL, from_ver NVARCHAR(32) NOT NULL, "
       "old_quote_id NVARCHAR(512) NOT NULL, to_ver NVARCHAR(32), new_quote_id NVARCHAR(512)",
       "ns, pack_id, from_ver, old_quote_id")
    _t(c, "kh_assumption",
       "ns TEXT NOT NULL, pack_id TEXT NOT NULL, code TEXT NOT NULL, n INTEGER, text TEXT, status TEXT, "
       "quote_id TEXT, quote_ver TEXT, superseded_by TEXT, note TEXT, actor_json TEXT, created_ms INTEGER, "
       "updated_ms INTEGER, rev INTEGER NOT NULL DEFAULT 1",
       "ns NVARCHAR(64) NOT NULL, pack_id NVARCHAR(64) NOT NULL, code NVARCHAR(32) NOT NULL, n BIGINT, "
       "text NCLOB, status NVARCHAR(16), quote_id NVARCHAR(512), quote_ver NVARCHAR(32), "
       "superseded_by NVARCHAR(32), note NCLOB, actor_json NCLOB, created_ms BIGINT, updated_ms BIGINT, "
       "rev BIGINT NOT NULL", "ns, pack_id, code")
    _t(c, "kh_pack_question",
       "ns TEXT NOT NULL, pack_id TEXT NOT NULL, question_id TEXT NOT NULL, n INTEGER, text TEXT, text_sha TEXT, "
       "status TEXT, answer TEXT, quote_id TEXT, quote_ver TEXT, asked_n INTEGER NOT NULL DEFAULT 1, "
       "indexed INTEGER NOT NULL DEFAULT 0, actor_json TEXT, created_ms INTEGER, updated_ms INTEGER, "
       "rev INTEGER NOT NULL DEFAULT 1",
       "ns NVARCHAR(64) NOT NULL, pack_id NVARCHAR(64) NOT NULL, question_id NVARCHAR(32) NOT NULL, n BIGINT, "
       "text NCLOB, text_sha NVARCHAR(64), status NVARCHAR(16), answer NCLOB, quote_id NVARCHAR(512), "
       "quote_ver NVARCHAR(32), asked_n BIGINT NOT NULL, indexed INTEGER NOT NULL, actor_json NCLOB, "
       "created_ms BIGINT, updated_ms BIGINT, rev BIGINT NOT NULL", "ns, pack_id, question_id")
    sqldb.create_index(c, "kh_pack_question_sha", "kh_pack_question", ["ns", "pack_id", "text_sha"], unique=True)
    sqldb.create_index(c, "kh_quote_map_to", "kh_quote_map", ["ns", "pack_id", "to_ver"])


def init(c) -> None:
    sqldb.migrate(c, NAME, [(1, _m1)])
    try:  # the namespace /context/step `packs` tier
        from app import caps

        ret = caps.module("retrieval")
        provs = getattr(ret, "PROVIDERS", None) if ret is not None else None
        if isinstance(provs, dict):
            provs["packs"] = retrieval_provider
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def _pol(ctx: Ctx) -> dict:
    s = ctx.settings("packs")
    return s if isinstance(s, dict) else {}


def _require_read(ctx: Ctx) -> None:
    ctx.require("packs", "read")
    if not core.acl_allows(ctx.role, _pol(ctx).get("read_roles")):
        raise CapError("forbidden", f"role '{ctx.role}' may not read packs in this namespace")


def _require_write(ctx: Ctx) -> None:
    ctx.require("packs", "write")
    if not core.acl_allows(ctx.role, _pol(ctx).get("write_roles")):
        raise CapError("forbidden", f"role '{ctx.role}' may not write packs in this namespace")


def _fenced(ctx: Ctx) -> bool:
    return bool(_pol(ctx).get("fenced"))


def _if_match(ctx: Ctx, request: Request) -> int | None:
    return core.require_if_match(request, bool(_pol(ctx).get("require_if_match")))


def may_confirm(ctx: Ctx) -> bool:
    """Admin; else `settings.packs.confirm_policy {roles, emails}` when declared; else role `packs:confirm`."""
    if ctx.is_admin:
        return True
    pol = _pol(ctx).get("confirm_policy")
    if isinstance(pol, dict) and (pol.get("roles") or pol.get("emails")):
        emails = {str(e).strip().lower() for e in pol.get("emails") or []}
        return ctx.role in (pol.get("roles") or []) or (ctx.caller.email or "").lower() in emails
    return ctx.allows("packs", "confirm")


def _threshold(ctx: Ctx, v: Any) -> float:
    if v is None:
        v = _pol(ctx).get("duplicate_threshold", DUP_THRESHOLD)
    try:
        t = float(v)
    except (TypeError, ValueError):
        raise CapError("invalid", "threshold must be a number")
    if isinstance(v, bool) or not DUP_MIN <= t <= DUP_MAX:
        raise CapError("invalid", f"threshold must be in [{DUP_MIN}..{DUP_MAX}]")
    return t


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    """NFC, LF line ends, rstrip every line and the end, no NUL."""
    t = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return "\n".join(line.rstrip() for line in t.split("\n")).rstrip()


def _norm_quote(text: str) -> str:
    return unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


def label_of(n: int, k: int) -> str:
    return f"v{int(n)}.{int(k)}"


def parse_label(v: Any) -> tuple[int, int]:
    m = LABEL_RE.match(str(v or "").strip())
    if not m:
        raise CapError("invalid", "version must look like vN.k (or latest)")
    return int(m.group(1)), int(m.group(2) or 0)


def _check_pack(pack: str) -> str:
    if not PACK_RE.match(pack or ""):
        raise CapError("invalid", f"pack id must match {PACK_RE.pattern}")
    return pack


def _check_path(p: Any) -> str:
    if not isinstance(p, str) or not 1 <= len(p) <= PATH_MAX or "#" in p or any(ord(ch) < 32 or ord(ch) == 127
                                                                                for ch in p):
        raise CapError("invalid", f"path must be 1-{PATH_MAX} characters without '#' or control characters")
    return p


def _check_page(v: Any) -> int:
    return core.parse_int(v, "page", 1, 0, PAGE_MAX)


def _parse_parts(raw: Any) -> list[tuple[str, int, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise CapError("invalid", "parts must be a list of {path, page?, text}")
    if len(raw) > MAX_PARTS:
        raise CapError("too_large", f"at most {MAX_PARTS} parts per version", extra={"limit": MAX_PARTS})
    out, seen = [], set()
    for i, p in enumerate(raw):
        if not isinstance(p, dict) or not isinstance(p.get("text"), str):
            raise CapError("invalid", f"parts[{i}] must be {{path, page?, text}}")
        path, page = _check_path(p.get("path")), _check_page(p.get("page"))
        if (path, page) in seen:
            raise CapError("invalid", f"parts[{i}] repeats {path} page {page}")
        seen.add((path, page))
        text = normalize(p["text"])
        if len(text) > PART_MAX_CHARS:
            raise CapError("too_large", f"part {path} page {page} has {len(text)} characters; the limit is "
                                        f"{PART_MAX_CHARS}", extra={"limit": PART_MAX_CHARS})
        out.append((path, page, text))
    return out


def _parse_remove(raw: Any) -> list[tuple[str, int | None]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise CapError("invalid", "remove must be a list of paths or {path, page?}")
    out = []
    for r in raw:
        if isinstance(r, str):
            out.append((_check_path(r), None))
        elif isinstance(r, dict):
            out.append((_check_path(r.get("path")),
                        None if r.get("page") is None else _check_page(r.get("page"))))
        else:
            raise CapError("invalid", "remove must be a list of paths or {path, page?}")
    return out


def version_sha(parts: dict) -> str:
    return core.sha256_json(sorted([[path, page, v[0]] for (path, page), v in parts.items()]))


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------


def _head(c, ns: str, pack: str):
    return c.execute("SELECT * FROM kh_pack_head WHERE ns=? AND pack_id=?", (ns, pack)).fetchone()


def _visible_head(c, ctx: Ctx, pack: str):
    h = _head(c, ctx.ns, pack)
    if h is None or not ctx.may_project(h["project"]):
        raise CapError("not_found", f"pack '{pack}' not found")
    return h


def _ver(c, ns: str, pack: str, n: int, k: int):
    return c.execute("SELECT * FROM kh_pack WHERE ns=? AND pack_id=? AND version=? AND addendum=?",
                     (ns, pack, n, k)).fetchone()


def _resolve_ver(c, ns: str, pack: str, head, ver: Any, frozen_only: bool = False):
    """`vN.k` or latest/blank (latest frozen; else the newest draft unless frozen_only)."""
    if ver in (None, "", "latest"):
        if head["latest_ver"]:
            return _ver(c, ns, pack, int(head["latest_ver"]), int(head["latest_add"]))
        if frozen_only:
            raise CapError("not_found", f"pack '{pack}' has no frozen version")
        r = c.execute("SELECT * FROM kh_pack WHERE ns=? AND pack_id=? ORDER BY version DESC, addendum DESC LIMIT 1",
                      (ns, pack)).fetchone()
        if r is None:
            raise CapError("not_found", f"pack '{pack}' has no version")
        return r
    n, k = parse_label(ver)
    r = _ver(c, ns, pack, n, k)
    if r is None:
        raise CapError("not_found", f"version {label_of(n, k)} of pack '{pack}' not found")
    return r


def _part_rows(c, ns: str, pack: str, n: int, k: int) -> list:
    return c.execute("SELECT path, page, text_ref, sha256, chars FROM kh_pack_part WHERE ns=? AND pack_id=? AND "
                     "version=? AND addendum=? ORDER BY path, page", (ns, pack, n, k)).fetchall()


def _blob(c, ns: str, sha: str) -> str:
    r = c.execute("SELECT text FROM kh_pack_blob WHERE ns=? AND sha256=?", (ns, sha)).fetchone()
    return r[0] if r else ""


def _texts(c, ns: str, pack: str, n: int, k: int) -> dict[tuple[str, int], str]:
    return {(r[0], int(r[1])): _blob(c, ns, r[2]) for r in _part_rows(c, ns, pack, n, k)}


def _version_view(r) -> dict:
    return {"label": r["label"], "state": r["state"], "sha256": r["sha256"], "base_label": r["base_label"],
            "parts_n": int(r["parts_n"] or 0), "chars": int(r["chars"] or 0), "index_state": r["index_state"],
            "created_ms": r["created_ms"], "frozen_ms": r["frozen_ms"], "rev": int(r["rev"])}


def _head_view(h) -> dict:
    latest = label_of(h["latest_ver"], h["latest_add"]) if h["latest_ver"] else None
    return {"pack_id": h["pack_id"], "project": h["project"] or "", "title": h["title"], "latest_version": latest,
            "created_ms": h["created_ms"], "updated_ms": h["updated_ms"], "rev": int(h["rev"])}


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def _overlay(c, ns: str, base: dict, parts: list, remove: list) -> dict:
    """{(path, page): (sha, chars)} = base minus removals plus parts; new texts go to the blob store."""
    out = dict(base)
    for path, page in remove:
        for key in [k for k in out if k[0] == path and (page is None or k[1] == page)]:
            out.pop(key)
    for path, page, text in parts:
        sha = core.sha256_text(text)
        sqldb.insert_new(c, "kh_pack_blob", {"ns": ns, "sha256": sha, "text": text})
        out[(path, page)] = (sha, len(text))
    if len(out) > MAX_PARTS:
        raise CapError("too_large", f"a version holds at most {MAX_PARTS} parts", extra={"limit": MAX_PARTS})
    total = sum(v[1] for v in out.values())
    if total > VERSION_MAX_CHARS:
        raise CapError("too_large", f"a version holds at most {VERSION_MAX_CHARS} characters",
                       extra={"limit": VERSION_MAX_CHARS})
    return out


def _base_parts(c, ns: str, pack: str, n: int, k: int) -> dict:
    return {(r[0], int(r[1])): (r[3], int(r[4] or 0)) for r in _part_rows(c, ns, pack, n, k)}


def _write_parts(c, ns: str, pack: str, n: int, k: int, parts: dict) -> None:
    c.execute("DELETE FROM kh_pack_part WHERE ns=? AND pack_id=? AND version=? AND addendum=?", (ns, pack, n, k))
    for (path, page), (sha, chars) in sorted(parts.items()):
        sqldb.insert_new(c, "kh_pack_part", {"ns": ns, "pack_id": pack, "version": n, "addendum": k, "path": path,
                                             "page": page, "text_ref": sha, "sha256": sha, "chars": chars})


def create_version(ctx: Ctx, pack: str, body: dict, addendum_of: str | None = None) -> dict:
    parts = _parse_parts(body.get("parts"))
    remove = _parse_remove(body.get("remove"))
    title = body.get("title")
    if title is not None and (not isinstance(title, str) or len(title) > TITLE_MAX):
        raise CapError("invalid", f"title must be a string of at most {TITLE_MAX} characters")
    base = body.get("base") if addendum_of is None else addendum_of
    if base is not None and not isinstance(base, str):
        raise CapError("invalid", "base must be a version label")
    with sqldb.connect() as c:
        core.fence(ctx, c, required=_fenced(ctx))
        now = sqldb.now_ms(c)
        h = _head(c, ctx.ns, pack)
        if h is None:
            if addendum_of is not None:
                raise CapError("not_found", f"pack '{pack}' not found")
            project = ctx.require_project(body.get("project"))
            sqldb.insert_new(c, "kh_pack_head", {"ns": ctx.ns, "pack_id": pack, "project": project, "title": title,
                                                 "latest_ver": 0, "latest_add": 0, "actor_json": ctx.actor_json(),
                                                 "created_ms": now, "updated_ms": now, "rev": 1})
            h = _head(c, ctx.ns, pack)
        elif not ctx.may_project(h["project"]):
            raise CapError("not_found", f"pack '{pack}' not found")
        elif body.get("project") not in (None, "", h["project"]):
            raise CapError("invalid", f"pack '{pack}' belongs to project '{h['project']}'")
        base_parts: dict = {}
        base_label = None
        if base is not None:
            b = _resolve_ver(c, ctx.ns, pack, h, base, frozen_only=True)
            if b["state"] == "draft":
                raise CapError("invalid", f"base {b['label']} is a draft; name a frozen version")
            base_parts, base_label = _base_parts(c, ctx.ns, pack, int(b["version"]), int(b["addendum"])), b["label"]
        if addendum_of is not None:
            n = int(b["version"])
            k = sqldb.counter_add(c, f"pka:{ctx.ns}:{pack}:{n}")
        else:
            n, k = sqldb.counter_add(c, f"pkv:{ctx.ns}:{pack}"), 0
        merged = _overlay(c, ctx.ns, base_parts, parts, remove)
        label = label_of(n, k)
        sqldb.insert_new(c, "kh_pack", {"ns": ctx.ns, "pack_id": pack, "version": n, "addendum": k, "label": label,
                                        "sha256": version_sha(merged), "state": "draft", "base_label": base_label,
                                        "parts_n": len(merged), "chars": sum(v[1] for v in merged.values()),
                                        "index_state": "none", "actor_json": ctx.actor_json(), "created_ms": now,
                                        "frozen_ms": None, "rev": 1})
        _write_parts(c, ctx.ns, pack, n, k, merged)
        if title is not None:
            c.execute("UPDATE kh_pack_head SET title=?, updated_ms=? WHERE ns=? AND pack_id=?",
                      (title, now, ctx.ns, pack))
        out = {"pack_id": pack, "project": h["project"] or "", **_version_view(_ver(c, ctx.ns, pack, n, k))}
    core.emit(ctx.ns, "kh.pack_version", {"pack": pack, "version": out["label"], "state": "draft",
                                          "sha256": out["sha256"], "base_label": base_label,
                                          "parts_n": out["parts_n"]}, ctx, project=out["project"])
    return out


def patch_version(ctx: Ctx, pack: str, ver: str, body: dict, expected: int | None) -> dict:
    parts = _parse_parts(body.get("parts"))
    remove = _parse_remove(body.get("remove"))
    title = body.get("title")
    if title is not None and (not isinstance(title, str) or len(title) > TITLE_MAX):
        raise CapError("invalid", f"title must be a string of at most {TITLE_MAX} characters")
    with sqldb.connect() as c:
        core.fence(ctx, c, required=_fenced(ctx))
        now = sqldb.now_ms(c)
        h = _visible_head(c, ctx, pack)
        r = _resolve_ver(c, ctx.ns, pack, h, ver)
        if r["state"] != "draft":
            raise CapError("pack_immutable", f"version {r['label']} is {r['state']}; only drafts change")
        n, k = int(r["version"]), int(r["addendum"])
        rev = int(r["rev"]) if expected is None or expected < 0 else expected
        merged = _overlay(c, ctx.ns, _base_parts(c, ctx.ns, pack, n, k), parts, remove)
        cur = c.execute("UPDATE kh_pack SET sha256=?, parts_n=?, chars=?, actor_json=?, rev=rev+1 WHERE ns=? AND "
                        "pack_id=? AND version=? AND addendum=? AND state='draft' AND rev=?",
                        (version_sha(merged), len(merged), sum(v[1] for v in merged.values()), ctx.actor_json(),
                         ctx.ns, pack, n, k, rev))
        if not cur.rowcount or cur.rowcount <= 0:
            now_r = _ver(c, ctx.ns, pack, n, k)
            if now_r["state"] != "draft":
                raise CapError("pack_immutable", f"version {now_r['label']} is {now_r['state']}")
            raise core.write_conflict(ctx.ns, f"version {now_r['label']}", int(now_r["rev"]), rev)
        _write_parts(c, ctx.ns, pack, n, k, merged)
        if title is not None:
            c.execute("UPDATE kh_pack_head SET title=?, updated_ms=? WHERE ns=? AND pack_id=?",
                      (title, now, ctx.ns, pack))
        out = {"pack_id": pack, "project": h["project"] or "", **_version_view(_ver(c, ctx.ns, pack, n, k))}
    core.emit(ctx.ns, "kh.pack_version", {"pack": pack, "version": out["label"], "state": "draft",
                                          "sha256": out["sha256"], "parts_n": out["parts_n"]}, ctx,
              project=out["project"])
    return out


# -- chunking ---------------------------------------------------------------------------------------------------

_SENT_END = re.compile(r"(?<=[.!?])\s+|\n\s*\n")


def _sentences(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in _SENT_END.finditer(text):
        if text[start:m.start()].strip():
            spans.append((start, m.start()))
        start = m.end()
    if text[start:].strip():
        spans.append((start, len(text)))
    out = []
    for s, e in spans:
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1].isspace():
            e -= 1
        if e > s:
            out.append((s, e))
    return out


def _windows(text: str, s: int, e: int) -> list[tuple[int, int]]:
    out = []
    while e - s > CHUNK_CHARS:
        cut = s + CHUNK_CHARS
        ws = max(text.rfind(" ", s, cut), text.rfind("\n", s, cut))
        if ws > s + CHUNK_CHARS // 2:
            cut = ws
        out.append((s, cut))
        s = cut
        while s < e and text[s].isspace():
            s += 1
    if e > s:
        out.append((s, e))
    return out


def chunk_spans(text: str, model=None) -> list[tuple[int, int]]:
    """(offset, end) chunks of a normalized part, each <= CHUNK_CHARS. Semantic grouping of sentences (adjacent
    similarity below mean - std starts a new chunk) when an embedder is given, else greedy packing; long chunks
    are split into whitespace-aware windows."""
    sents = _sentences(text)
    if not sents:
        return []
    groups: list[list[tuple[int, int]]] = []
    if model is not None and len(sents) > 1:
        import numpy as np

        emb = np.asarray(model.encode([text[s:e] for s, e in sents], normalize_embeddings=True), dtype=np.float32)
        sims = np.sum(emb[:-1] * emb[1:], axis=1)
        cutoff = float(np.mean(sims) - np.std(sims))
        cur = [sents[0]]
        for i, sim in enumerate(sims):
            nxt = sents[i + 1]
            if sim < cutoff or nxt[1] - cur[0][0] > CHUNK_CHARS:
                groups.append(cur)
                cur = []
            cur.append(nxt)
        groups.append(cur)
    else:
        cur = [sents[0]]
        for nxt in sents[1:]:
            if nxt[1] - cur[0][0] > CHUNK_CHARS:
                groups.append(cur)
                cur = []
            cur.append(nxt)
        groups.append(cur)
    out = []
    for g in groups:
        out.extend(_windows(text, g[0][0], g[-1][1]))
    return out


def _chunk_model():
    try:
        from app.embeddings import get_embedder

        emb = get_embedder()
        if getattr(emb, "loaded", True) is False:
            return None
        return emb
    except Exception:  # noqa: BLE001
        return None


def _chunks_for(texts: dict) -> dict:
    model = _chunk_model()
    out = {}
    for key, text in texts.items():
        try:
            out[key] = chunk_spans(text, model)
        except Exception:  # noqa: BLE001  (embedder failed: plain windows)
            out[key] = chunk_spans(text, None)
    return out


# -- quotes ---------------------------------------------------------------------------------------------------


def _qid(path: str, page: int, off: int) -> str:
    return f"{path}#{page}#{off}"


def _assign_qid(c, ns: str, pack: str, label: str, path: str, page: int, off: int, length: int) -> tuple[str, bool]:
    """(quote_id, exists). `path#page#offset`, or `...~length` when that id is taken by another length."""
    base = _qid(path, page, off)
    r = c.execute("SELECT q_length FROM kh_quote WHERE ns=? AND pack_id=? AND version=? AND quote_id=?",
                  (ns, pack, label, base)).fetchone()
    if r is None:
        return base, False
    if int(r[0]) == length:
        return base, True
    alt = f"{base}~{length}"
    r = c.execute("SELECT 1 FROM kh_quote WHERE ns=? AND pack_id=? AND version=? AND quote_id=?",
                  (ns, pack, label, alt)).fetchone()
    return alt, r is not None


def _insert_quote(c, ns, pack, label, qid, path, page, off, text, origin, now) -> None:
    sqldb.insert_new(c, "kh_quote", {"ns": ns, "pack_id": pack, "version": label, "quote_id": qid, "path": path,
                                     "page": page, "q_offset": off, "q_length": len(text), "text": text,
                                     "sha256": core.sha256_text(text), "origin": origin, "created_ms": now})


def _quote_view(pack: str, r) -> dict:
    return {"pack_id": pack, "version": r["version"], "quote_id": r["quote_id"], "path": r["path"],
            "page": int(r["page"]), "offset": int(r["q_offset"]), "length": int(r["q_length"]), "text": r["text"],
            "sha256": r["sha256"], "origin": r["origin"], "created_ms": r["created_ms"]}


def _find_nearest(text: str, needle: str, near: int | None) -> int:
    best, pos = -1, text.find(needle)
    while pos >= 0:
        if near is None:
            return pos
        if best < 0 or abs(pos - near) < abs(best - near):
            best = pos
        if pos > near:
            break
        pos = text.find(needle, pos + 1)
    return best


def _reanchor(q, texts: dict) -> tuple[str, int, int] | None:
    """Exact text search: same path+page nearest offset, then same path, then any part."""
    needle, path, page, off = q["text"], q["path"], int(q["page"]), int(q["q_offset"])
    if not needle:
        return None
    t = texts.get((path, page))
    if t is not None:
        pos = _find_nearest(t, needle, off)
        if pos >= 0:
            return path, page, pos
    for (p, pg) in sorted(texts):
        if p == path and pg != page:
            pos = texts[(p, pg)].find(needle)
            if pos >= 0:
                return p, pg, pos
    for (p, pg) in sorted(texts):
        if p != path:
            pos = texts[(p, pg)].find(needle)
            if pos >= 0:
                return p, pg, pos
    return None


def freeze(ctx: Ctx, pack: str, ver: str, expected: int | None) -> dict:
    with sqldb.connect() as c:
        h = _visible_head(c, ctx, pack)
        r = _resolve_ver(c, ctx.ns, pack, h, ver)
        if r["state"] != "draft":
            raise CapError("pack_immutable", f"version {r['label']} is already {r['state']}")
        n, k, label = int(r["version"]), int(r["addendum"]), r["label"]
        rev, sha = int(r["rev"]), r["sha256"]
        if expected is not None and expected >= 0 and expected != rev:
            raise core.write_conflict(ctx.ns, f"version {label}", rev, expected)
        texts = _texts(c, ctx.ns, pack, n, k)
    chunks = _chunks_for(texts)  # outside the transaction
    stats = {"quotes": 0, "reanchored": 0, "orphaned": 0, "superseded": None}
    with sqldb.connect() as c:
        core.fence(ctx, c, required=_fenced(ctx))
        now = sqldb.now_ms(c)
        c.execute("UPDATE kh_pack_head SET rev=rev+1, updated_ms=? WHERE ns=? AND pack_id=?", (now, ctx.ns, pack))
        h = _head(c, ctx.ns, pack)
        cur = c.execute("UPDATE kh_pack SET state='frozen', frozen_ms=?, index_state='pending', rev=rev+1 WHERE "
                        "ns=? AND pack_id=? AND version=? AND addendum=? AND state='draft' AND rev=? AND sha256=?",
                        (now, ctx.ns, pack, n, k, rev, sha))
        if not cur.rowcount or cur.rowcount <= 0:
            now_r = _ver(c, ctx.ns, pack, n, k)
            if now_r["state"] != "draft":
                raise CapError("pack_immutable", f"version {label} is already {now_r['state']}")
            raise core.write_conflict(ctx.ns, f"version {label}", int(now_r["rev"]), rev)
        prev = (int(h["latest_ver"]), int(h["latest_add"])) if h["latest_ver"] else None
        if prev is not None and (n, k) <= prev:
            raise CapError("version_order", f"{label} is not newer than the latest frozen {label_of(*prev)}",
                           extra={"latest": label_of(*prev)})
        for (path, page), spans in chunks.items():
            t = texts[(path, page)]
            for s, e in spans:
                _insert_quote(c, ctx.ns, pack, label, _qid(path, page, s), path, page, s, t[s:e], "chunk", now)
                stats["quotes"] += 1
        if prev is not None:
            plabel = label_of(*prev)
            c.execute("UPDATE kh_pack SET state='superseded', rev=rev+1 WHERE ns=? AND pack_id=? AND version=? AND "
                      "addendum=? AND state='frozen'", (ctx.ns, pack, prev[0], prev[1]))
            stats["superseded"] = plabel
            for q in c.execute("SELECT * FROM kh_quote WHERE ns=? AND pack_id=? AND version=? ORDER BY quote_id",
                               (ctx.ns, pack, plabel)).fetchall():
                hit = _reanchor(q, texts)
                new_id = None
                if hit is not None:
                    p, pg, pos = hit
                    new_id, exists = _assign_qid(c, ctx.ns, pack, label, p, pg, pos, len(q["text"]))
                    if not exists:
                        _insert_quote(c, ctx.ns, pack, label, new_id, p, pg, pos, q["text"], "anchor", now)
                    stats["reanchored"] += 1
                else:
                    stats["orphaned"] += 1
                sqldb.upsert(c, "kh_quote_map", ["ns", "pack_id", "from_ver", "old_quote_id"],
                             {"ns": ctx.ns, "pack_id": pack, "from_ver": plabel, "old_quote_id": q["quote_id"],
                              "to_ver": label, "new_quote_id": new_id})
        c.execute("UPDATE kh_pack_head SET latest_ver=?, latest_add=?, updated_ms=? WHERE ns=? AND pack_id=?",
                  (n, k, now, ctx.ns, pack))
        project = h["project"] or ""
    state = index_version(ctx.ns, pack, label, project)
    with sqldb.connect() as c:
        out = {"pack_id": pack, "project": project, **_version_view(_ver(c, ctx.ns, pack, n, k)), **stats}
    out["index_state"] = state
    core.emit(ctx.ns, "kh.pack_frozen", {"pack": pack, "version": label, "sha256": sha, **stats}, ctx,
              project=project)
    return out


# -- vector index -----------------------------------------------------------------------------------------------


def embed_on() -> bool:
    return (os.environ.get("KH_PACKS_EMBED") or "true").strip().lower() not in ("false", "0", "off", "no")


def packs_collection(ns: str) -> str:
    return f"ns_{ns}_packs"


def questions_collection(ns: str) -> str:
    return f"ns_{ns}_questions"


def _set_index_state(ns: str, pack: str, label: str, state: str) -> None:
    n, k = parse_label(label)
    with sqldb.connect() as c:
        c.execute("UPDATE kh_pack SET index_state=? WHERE ns=? AND pack_id=? AND version=? AND addendum=?",
                  (state, ns, pack, n, k))


def index_version(ns: str, pack: str, label: str, project: str) -> str:
    """Embed the chunk quotes of a frozen version into `ns_<ns>_packs`; returns the index_state."""
    if not embed_on():
        _set_index_state(ns, pack, label, "off")
        return "off"
    with sqldb.connect() as c:
        rows = c.execute("SELECT quote_id, path, page, text FROM kh_quote WHERE ns=? AND pack_id=? AND version=? AND "
                         "origin='chunk' ORDER BY quote_id", (ns, pack, label)).fetchall()
    try:
        from app import vector_store as vs
        from app.embeddings import EmbedderUnavailable, get_embedder
    except Exception:  # noqa: BLE001
        _set_index_state(ns, pack, label, "failed")
        return "failed"
    try:
        emb = get_embedder()
        coll = vs.get_or_create(packs_collection(ns))
        for i in range(0, len(rows), 256):
            batch = rows[i:i + 256]
            vecs = emb.encode([r[3] for r in batch], normalize_embeddings=True)
            vs.upsert_rows(coll, [f"{pack}@{label}#{r[0]}" for r in batch], list(vecs), [r[3] for r in batch],
                           [{"ns": ns, "pack_id": pack, "version": label, "quote_id": r[0], "project": project or "",
                             "path": r[1], "page": int(r[2])} for r in batch])
        state = "done"
    except EmbedderUnavailable:
        state = "no_model"
    except Exception:  # noqa: BLE001
        state = "failed"
    _set_index_state(ns, pack, label, state)
    return state


def reindex(ctx: Ctx, pack: str, ver: str) -> dict:
    with sqldb.connect() as c:
        h = _visible_head(c, ctx, pack)
        r = _resolve_ver(c, ctx.ns, pack, h, ver)
        if r["state"] == "draft":
            raise CapError("invalid", f"version {r['label']} is a draft; freeze it first")
        label, project = r["label"], h["project"] or ""
    state = index_version(ctx.ns, pack, label, project)
    with sqldb.connect() as c:
        out = {"pack_id": pack, "project": project, **_version_view(_ver(c, ctx.ns, pack, *parse_label(label)))}
    out["index_state"] = state
    return out


def retrieval_provider(ctx: Ctx, req: dict | None = None, **kw) -> list[dict]:
    """`packs` tier of `/v1/{ns}/context/step` (retrieval `PROVIDERS["packs"]`, fn(ctx, req)): chunk quotes of the
    latest frozen versions by meaning. `req` has project, query, limit (others ignored).

    Returns [{id, score, text, pack_id, version, quote_id, path, page, project}] (ids `<pack>@<ver>#<qid>`).
    """
    req = {**(req or {}), **kw}
    project, query, vector = req.get("project"), req.get("query"), req.get("vector")
    limit = int(req.get("limit") or 10)
    if not query and vector is None:
        return []
    from app import vector_store as vs

    coll = vs.get_existing(packs_collection(ctx.ns))
    if coll is None:
        return []
    if vector is None:
        from app.embeddings import get_embedder

        vector = get_embedder().encode(query, normalize_embeddings=True)
    hits = vs.query_vt(coll, vector, max(1, int(limit)) * 3, where={"ns": ctx.ns})
    out: list[dict] = []
    latest: dict[str, str | None] = {}
    with sqldb.connect() as c:
        for hit in hits:
            m = hit.get("metadata") or {}
            pk, proj = m.get("pack_id"), m.get("project") or ""
            if project and proj not in ("", project):
                continue
            if not ctx.may_project(proj):
                continue
            if pk not in latest:
                h = _head(c, ctx.ns, pk)
                latest[pk] = label_of(h["latest_ver"], h["latest_add"]) if h is not None and h["latest_ver"] else None
            if latest[pk] != m.get("version"):
                continue
            text = f"{m.get('path')} p{m.get('page')}: {hit.get('source_text') or ''}"
            out.append({"id": hit["vector_id"], "score": hit["score"], "text": text, "pack_id": pk,
                        "version": m.get("version"), "quote_id": m.get("quote_id"), "path": m.get("path"),
                        "page": m.get("page"), "project": proj})
            if len(out) >= limit:
                break
    return out


# -- reads ------------------------------------------------------------------------------------------------------


def list_packs(ctx: Ctx, after: str, limit: int, project: str = "") -> dict:
    q = "SELECT * FROM kh_pack_head WHERE ns=? AND pack_id > ?"
    params: list = [ctx.ns, after or ""]
    if project:
        q += " AND project=?"
        params.append(project)
    with sqldb.connect() as c:
        rows = c.execute(q + f" ORDER BY pack_id LIMIT {int(limit) + 1}", params).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    out = {"packs": [_head_view(r) for r in rows if ctx.may_project(r["project"])]}
    if more:
        out["next"] = rows[-1]["pack_id"]
    return out


def get_pack(ctx: Ctx, pack: str, ver: str | None, with_text: bool) -> dict:
    with sqldb.connect() as c:
        h = _visible_head(c, ctx, pack)
        r = _resolve_ver(c, ctx.ns, pack, h, ver)
        parts = []
        for p in _part_rows(c, ctx.ns, pack, int(r["version"]), int(r["addendum"])):
            v = {"path": p[0], "page": int(p[1]), "sha256": p[3], "chars": int(p[4] or 0)}
            if with_text:
                v["text"] = _blob(c, ctx.ns, p[2])
            parts.append(v)
        return {**_head_view(h), "version": _version_view(r), "parts": parts}


def list_versions(ctx: Ctx, pack: str) -> dict:
    with sqldb.connect() as c:
        h = _visible_head(c, ctx, pack)
        rows = c.execute("SELECT * FROM kh_pack WHERE ns=? AND pack_id=? ORDER BY version, addendum",
                         (ctx.ns, pack)).fetchall()
        return {**_head_view(h), "versions": [_version_view(r) for r in rows]}


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------


def cite(ctx: Ctx, pack: str, body: dict) -> tuple[int, dict]:
    path = _check_path(body.get("path"))
    page = _check_page(body.get("page"))
    offset = core.parse_int(body.get("offset"), "offset", None, 0)
    length = core.parse_int(body.get("length"), "length", None, 1, QUOTE_MAX_CHARS)
    text = body.get("text")
    if text is not None and (not isinstance(text, str) or not text):
        raise CapError("invalid", "text must be a non-empty string")
    if text is None and (offset is None or length is None):
        raise CapError("invalid", "cite by offset and length, or by exact text")
    with sqldb.connect() as c:
        h = _visible_head(c, ctx, pack)
        r = _resolve_ver(c, ctx.ns, pack, h, body.get("version"), frozen_only=True)
        if r["state"] == "draft":
            raise CapError("invalid", f"version {r['label']} is a draft; quotes cite frozen versions")
        label = r["label"]
        pr = c.execute("SELECT text_ref FROM kh_pack_part WHERE ns=? AND pack_id=? AND version=? AND addendum=? AND "
                       "path=? AND page=?", (ctx.ns, pack, r["version"], r["addendum"], path, page)).fetchone()
        if pr is None:
            raise CapError("not_found", f"{path} page {page} is not in {label}")
        part = _blob(c, ctx.ns, pr[0])
        if text is not None:
            needle = _norm_quote(text)
            if len(needle) > QUOTE_MAX_CHARS:
                raise CapError("too_large", f"a quote holds at most {QUOTE_MAX_CHARS} characters",
                               extra={"limit": QUOTE_MAX_CHARS})
            pos = _find_nearest(part, needle, offset)
            if pos < 0:
                raise CapError("not_found", f"text not found in {path} page {page} of {label}")
            offset, length = pos, len(needle)
        elif offset + length > len(part):
            raise CapError("invalid", f"offset+length is past the end of {path} page {page} ({len(part)} characters)")
        qtext = part[offset:offset + length]
        qid, exists = _assign_qid(c, ctx.ns, pack, label, path, page, offset, length)
        now = sqldb.now_ms(c)
        if not exists:
            core.fence(ctx, c, required=_fenced(ctx))
            _insert_quote(c, ctx.ns, pack, label, qid, path, page, offset, qtext, "cite", now)
        q = c.execute("SELECT * FROM kh_quote WHERE ns=? AND pack_id=? AND version=? AND quote_id=?",
                      (ctx.ns, pack, label, qid)).fetchone()
        return (200 if exists else 201), _quote_view(pack, q)


def list_quotes(ctx: Ctx, pack: str, ver: str | None, path: str | None, after: str, limit: int,
                origin: str = "") -> dict:
    with sqldb.connect() as c:
        h = _visible_head(c, ctx, pack)
        r = _resolve_ver(c, ctx.ns, pack, h, ver)
        q = "SELECT * FROM kh_quote WHERE ns=? AND pack_id=? AND version=? AND quote_id > ?"
        params: list = [ctx.ns, pack, r["label"], after or ""]
        if path:
            q += " AND path=?"
            params.append(path)
        if origin:
            q += " AND origin=?"
            params.append(origin)
        rows = c.execute(q + f" ORDER BY quote_id LIMIT {int(limit) + 1}", params).fetchall()
    more = len(rows) > limit
    out = {"pack_id": pack, "version": r["label"], "quotes": [_quote_view(pack, x) for x in rows[:limit]]}
    if more:
        out["next"] = rows[limit - 1]["quote_id"]
    return out


def _pack_of_quote(c, ctx: Ctx, quote_id: str) -> str:
    """The one visible pack holding a quote id (GET /v1/{ns}/quotes/{id} without ?pack)."""
    packs = [r[0] for r in c.execute("SELECT DISTINCT pack_id FROM kh_quote WHERE ns=? AND quote_id=? ORDER BY pack_id",
                                     (ctx.ns, quote_id)).fetchall()]
    packs = [p for p in packs if (h := _head(c, ctx.ns, p)) is not None and ctx.may_project(h["project"])]
    if not packs:
        raise CapError("not_found", f"quote '{quote_id}' not found")
    if len(packs) > 1:
        raise CapError("invalid", f"quote '{quote_id}' is in several packs ({', '.join(packs[:10])}); name ?pack=")
    return packs[0]


def resolve_quote(ctx: Ctx, quote_id: str, pack: str | None, ver: str | None) -> dict:
    with sqldb.connect() as c:
        if not pack:
            pack = _pack_of_quote(c, ctx, quote_id)
        h = _visible_head(c, ctx, pack)
        if ver in (None, ""):
            labels = [r[0] for r in c.execute("SELECT version FROM kh_quote WHERE ns=? AND pack_id=? AND quote_id=?",
                                              (ctx.ns, pack, quote_id)).fetchall()]
            if not labels:
                raise CapError("not_found", f"quote '{quote_id}' not found in pack '{pack}'")
            label = max(labels, key=parse_label)
        else:
            label = label_of(*parse_label(ver)) if ver != "latest" else _resolve_ver(c, ctx.ns, pack, h, None,
                                                                                     True)["label"]
        q = c.execute("SELECT * FROM kh_quote WHERE ns=? AND pack_id=? AND version=? AND quote_id=?",
                      (ctx.ns, pack, label, quote_id)).fetchone()
        if q is None:
            raise CapError("not_found", f"quote '{quote_id}' not found in {pack} {label}")
        requested = _quote_view(pack, q)
        cur_label, cur_id, hops, orphaned_at = label, quote_id, 0, None
        while hops < MAP_HOPS_MAX:
            m = c.execute("SELECT to_ver, new_quote_id FROM kh_quote_map WHERE ns=? AND pack_id=? AND from_ver=? AND "
                          "old_quote_id=?", (ctx.ns, pack, cur_label, cur_id)).fetchone()
            if m is None:
                break
            hops += 1
            if m[1] is None:
                orphaned_at = m[0]
                break
            cur_label, cur_id = m[0], m[1]
        latest_label = label_of(h["latest_ver"], h["latest_add"]) if h["latest_ver"] else None
        latest = None
        if orphaned_at is not None:
            status = "orphaned"
        else:
            lq = c.execute("SELECT * FROM kh_quote WHERE ns=? AND pack_id=? AND version=? AND quote_id=?",
                           (ctx.ns, pack, cur_label, cur_id)).fetchone()
            latest = _quote_view(pack, lq) if lq is not None else None
            if cur_label == latest_label:
                status = "current" if hops == 0 else "reanchored"
            else:
                status = "superseded"
    return {"requested": requested, "latest": latest, "status": status, "hops": hops, "orphaned_at": orphaned_at,
            "latest_version": latest_label}


def _check_quote_ref(c, ctx: Ctx, pack: str, head, quote_id: Any, quote_ver: Any) -> tuple[str | None, str | None]:
    if quote_id in (None, ""):
        return None, None
    if not isinstance(quote_id, str):
        raise CapError("invalid", "quote_id must be a string")
    if quote_ver in (None, "", "latest"):
        if not head["latest_ver"]:
            raise CapError("invalid", f"pack '{pack}' has no frozen version to quote")
        label = label_of(head["latest_ver"], head["latest_add"])
    else:
        label = label_of(*parse_label(quote_ver))
    if c.execute("SELECT 1 FROM kh_quote WHERE ns=? AND pack_id=? AND version=? AND quote_id=?",
                 (ctx.ns, pack, label, quote_id)).fetchone() is None:
        raise CapError("invalid", f"quote '{quote_id}' does not exist in {label}")
    return quote_id, label


# ---------------------------------------------------------------------------
# Assumptions
# ---------------------------------------------------------------------------


def _asm_view(pack: str, r) -> dict:
    return {"pack_id": pack, "code": r["code"], "n": int(r["n"]), "text": r["text"], "status": r["status"],
            "quote_id": r["quote_id"], "quote_ver": r["quote_ver"], "superseded_by": r["superseded_by"],
            "note": r["note"], "created_ms": r["created_ms"], "updated_ms": r["updated_ms"], "rev": int(r["rev"])}


def _text(v: Any, name: str, maximum: int, required: bool) -> str | None:
    if v is None:
        if required:
            raise CapError("invalid", f"{name} is required")
        return None
    if not isinstance(v, str) or not v.strip() or len(v) > maximum:
        raise CapError("invalid", f"{name} must be a non-empty string of at most {maximum} characters")
    return v.strip()


def add_assumption(ctx: Ctx, pack: str, body: dict) -> dict:
    text = _text(body.get("text"), "text", TEXT_MAX, True)
    note = _text(body.get("note"), "note", NOTE_MAX, False)
    with sqldb.connect() as c:
        core.fence(ctx, c, required=_fenced(ctx))
        h = _visible_head(c, ctx, pack)
        qid, qver = _check_quote_ref(c, ctx, pack, h, body.get("quote_id"), body.get("quote_ver"))
        now = sqldb.now_ms(c)
        n = sqldb.counter_add(c, f"asm:{ctx.ns}:{pack}")
        code = f"ASSUMED-{n}"
        sqldb.insert_new(c, "kh_assumption", {"ns": ctx.ns, "pack_id": pack, "code": code, "n": n, "text": text,
                                              "status": "open", "quote_id": qid, "quote_ver": qver,
                                              "superseded_by": None, "note": note, "actor_json": ctx.actor_json(),
                                              "created_ms": now, "updated_ms": now, "rev": 1})
        out = _asm_view(pack, c.execute("SELECT * FROM kh_assumption WHERE ns=? AND pack_id=? AND code=?",
                                        (ctx.ns, pack, code)).fetchone())
        project = h["project"] or ""
    core.emit(ctx.ns, "kh.assumption", {"pack": pack, "code": code, "status": "open", "from": None}, ctx,
              project=project)
    return out


def patch_assumption(ctx: Ctx, pack: str, code: str, body: dict, expected: int | None) -> dict:
    status = body.get("status")
    if status is not None and status not in ASM_STATUSES:
        raise CapError("invalid", f"status must be one of {', '.join(ASM_STATUSES)}")
    text = _text(body.get("text"), "text", TEXT_MAX, False)
    note = _text(body.get("note"), "note", NOTE_MAX, False) if body.get("note") is not None else None
    sup = body.get("superseded_by")
    if sup is not None and not isinstance(sup, str):
        raise CapError("invalid", "superseded_by must be an assumption code")
    edits = text is not None or "quote_id" in body or "quote_ver" in body or note is not None or sup is not None
    if status in ("confirmed", "refuted"):
        if not may_confirm(ctx):
            raise CapError("forbidden", "confirming or refuting an assumption needs the packs confirm policy")
        if edits:
            _require_write(ctx)
    else:
        _require_write(ctx)
    with sqldb.connect() as c:
        core.fence(ctx, c, required=_fenced(ctx))
        h = _visible_head(c, ctx, pack)
        r = c.execute("SELECT * FROM kh_assumption WHERE ns=? AND pack_id=? AND code=?", (ctx.ns, pack, code)).fetchone()
        if r is None:
            raise CapError("not_found", f"assumption '{code}' not found")
        if r["status"] == "superseded":
            raise CapError("assumption_final", f"assumption '{code}' is superseded")
        sets: dict = {"updated_ms": sqldb.now_ms(c), "actor_json": ctx.actor_json()}
        if text is not None:
            sets["text"] = text
        if note is not None:
            sets["note"] = note
        if "quote_id" in body or "quote_ver" in body:
            sets["quote_id"], sets["quote_ver"] = _check_quote_ref(c, ctx, pack, h, body.get("quote_id"),
                                                                   body.get("quote_ver"))
        if sup is not None:
            if c.execute("SELECT 1 FROM kh_assumption WHERE ns=? AND pack_id=? AND code=?",
                         (ctx.ns, pack, sup)).fetchone() is None or sup == code:
                raise CapError("invalid", f"superseded_by '{sup}' is not another assumption of this pack")
            sets["superseded_by"] = sup
            status = status or "superseded"
        new = status or r["status"]
        changed_content = (text is not None and text != r["text"]) or (
            "quote_id" in sets and (sets["quote_id"], sets["quote_ver"]) != (r["quote_id"], r["quote_ver"]))
        if status is None and changed_content and r["status"] in ("confirmed", "refuted"):
            new = "open"  # a confirmed/refuted assumption whose text or quote changes drops to open
        sets["status"] = new
        rev = core.cas_update(c, "kh_assumption", {"ns": ctx.ns, "pack_id": pack, "code": code}, sets, expected,
                              ns=ctx.ns, what=f"assumption '{code}'")
        out = _asm_view(pack, c.execute("SELECT * FROM kh_assumption WHERE ns=? AND pack_id=? AND code=?",
                                        (ctx.ns, pack, code)).fetchone())
        out["rev"] = rev
        project, old = h["project"] or "", r["status"]
    core.emit(ctx.ns, "kh.assumption", {"pack": pack, "code": code, "status": new, "from": old}, ctx, project=project)
    return out


def list_assumptions(ctx: Ctx, pack: str, status: str | None, limit: int) -> dict:
    with sqldb.connect() as c:
        _visible_head(c, ctx, pack)
        q, params = "SELECT * FROM kh_assumption WHERE ns=? AND pack_id=?", [ctx.ns, pack]
        if status:
            st = core.csv(status)
            q += f" AND status IN ({','.join('?' for _ in st)})"
            params += st
        rows = c.execute(q + f" ORDER BY n LIMIT {int(limit)}", params).fetchall()
        return {"pack_id": pack, "assumptions": [_asm_view(pack, r) for r in rows]}


def get_assumption(ctx: Ctx, pack: str, code: str) -> dict:
    with sqldb.connect() as c:
        _visible_head(c, ctx, pack)
        r = c.execute("SELECT * FROM kh_assumption WHERE ns=? AND pack_id=? AND code=?", (ctx.ns, pack, code)).fetchone()
        if r is None:
            raise CapError("not_found", f"assumption '{code}' not found")
        return _asm_view(pack, r)


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
NUMBER_WORDS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
                "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
                "hundred": "100", "thousand": "1000", "once": "x1", "twice": "x2", "thrice": "x3"}
NEGATIONS = {"not", "no", "never", "without", "cannot", "neither", "nor", "none"}
OPPOSITES = [
    ("above", "below"), ("over", "under"), ("min", "max"), ("minimum", "maximum"), ("include", "exclude"),
    ("read", "write"), ("enable", "disable"), ("login", "logout"), ("input", "output"), ("before", "after"),
    ("first", "last"), ("start", "end"), ("open", "close"), ("increase", "decrease"), ("add", "remove"),
    ("allow", "deny"), ("accept", "reject"), ("true", "false"), ("import", "export"), ("upload", "download"),
    ("push", "pull"), ("send", "receive"), ("buy", "sell"), ("public", "private"), ("internal", "external"),
    ("success", "failure"), ("pass", "fail"), ("valid", "invalid"), ("encrypt", "decrypt"), ("lock", "unlock"),
    ("mount", "unmount"), ("install", "uninstall"), ("create", "delete"), ("show", "hide"), ("start", "stop"),
    ("begin", "finish"), ("high", "low"), ("higher", "lower"), ("more", "less"), ("most", "least"),
    ("large", "small"), ("larger", "smaller"), ("long", "short"), ("fast", "slow"), ("early", "late"),
    ("old", "new"), ("inside", "outside"), ("upper", "lower"), ("left", "right"), ("top", "bottom"),
    ("front", "back"), ("positive", "negative"), ("plus", "minus"), ("sync", "async"), ("online", "offline"),
    ("connect", "disconnect"), ("attach", "detach"), ("inbound", "outbound"), ("ingress", "egress"),
    ("client", "server"), ("source", "target"), ("parent", "child"), ("debit", "credit"), ("income", "expense"),
    ("profit", "loss"), ("win", "lose"), ("approve", "reject"), ("grant", "revoke"), ("subscribe", "unsubscribe"),
    ("mandatory", "optional"), ("required", "optional"), ("permanent", "temporary"), ("manual", "automatic"),
    ("static", "dynamic"), ("explicit", "implicit"), ("absolute", "relative"), ("local", "remote"),
    ("horizontal", "vertical"), ("ascending", "descending"), ("earliest", "latest"), ("oldest", "newest"),
    ("weekday", "weekend"), ("day", "night"), ("morning", "evening"), ("buyer", "seller"), ("lessor", "lessee"),
    ("employer", "employee"), ("import", "export"), ("allowed", "forbidden"), ("include", "omit"),
    ("increment", "decrement"), ("maximize", "minimize"), ("upgrade", "downgrade"), ("encode", "decode"),
    ("compress", "decompress"), ("join", "leave"), ("arrive", "depart"), ("arrival", "departure"),
]


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(unicodedata.normalize("NFC", text or "").lower())


def text_sha(text: str) -> str:
    return core.sha256_text(" ".join(tokens(text)))


def stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s", "e"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[:-len(suf)]
    return w


_OPP = [(stem(a), stem(b)) for a, b in OPPOSITES]


def _numbers(toks: list[str]) -> set[str]:
    out = set()
    for t in toks:
        if re.fullmatch(r"\d+(?:\.\d+)?", t):
            out.add(str(float(t)).rstrip("0").rstrip(".") if "." in t else str(int(t)))
        elif t in NUMBER_WORDS:
            out.add(NUMBER_WORDS[t])
    return out


def _negations(toks: list[str]) -> int:
    return sum(1 for t in toks if t in NEGATIONS or t.endswith("n't"))


def contrast(a: str, b: str) -> str | None:
    """Why two similar questions differ in meaning, or None: number sets, negation parity, opposite pairs."""
    ta, tb = tokens(a), tokens(b)
    if _numbers(ta) != _numbers(tb):
        return "numbers"
    if _negations(ta) % 2 != _negations(tb) % 2:
        return "negation"
    sa, sb = {stem(t) for t in ta}, {stem(t) for t in tb}
    for x, y in _OPP:
        if (x in sa and y not in sa and y in sb and x not in sb) or (y in sa and x not in sa and x in sb
                                                                      and y not in sb):
            return f"opposite:{x}/{y}"
    return None


def _q_view(pack: str, r) -> dict:
    return {"pack_id": pack, "question_id": r["question_id"], "n": int(r["n"]), "text": r["text"],
            "status": r["status"], "answer": r["answer"], "quote_id": r["quote_id"], "quote_ver": r["quote_ver"],
            "asked_n": int(r["asked_n"]), "indexed": bool(r["indexed"]), "created_ms": r["created_ms"],
            "updated_ms": r["updated_ms"], "rev": int(r["rev"])}


def _q_by_sha(c, ns: str, pack: str, sha: str):
    return c.execute("SELECT * FROM kh_pack_question WHERE ns=? AND pack_id=? AND text_sha=?",
                     (ns, pack, sha)).fetchone()


def _semantic(ctx: Ctx, pack: str, text: str, threshold: float) -> tuple[Any, list[dict], Any]:
    """(best duplicate hit {question_id, score} or None, candidates, vector or None)."""
    try:
        from app import vector_store as vs
        from app.embeddings import EmbedderUnavailable, get_embedder
    except Exception:  # noqa: BLE001
        return None, [], None
    try:
        vec = get_embedder().encode(text, normalize_embeddings=True)
    except EmbedderUnavailable:
        return None, [], None
    coll = vs.get_existing(questions_collection(ctx.ns))
    if coll is None:
        return None, [], vec
    hits = vs.query_vt(coll, vec, LOOKUP_K, where={"$and": [{"ns": ctx.ns}, {"pack_id": pack}]})
    cands, best = [], None
    for hit in hits:
        qid = (hit.get("metadata") or {}).get("question_id")
        cands.append({"question_id": qid, "score": hit["score"], "text": hit.get("source_text") or ""})
    return best, cands, vec


def _index_question(ns: str, pack: str, qid: str, text: str, project: str, vec) -> bool:
    try:
        from app import vector_store as vs

        coll = vs.get_or_create(questions_collection(ns))
        vs.upsert_one(coll, f"{pack}#{qid}", vec, text, {"ns": ns, "pack_id": pack, "question_id": qid,
                                                         "project": project or ""})
    except Exception:  # noqa: BLE001
        return False
    with sqldb.connect() as c:
        c.execute("UPDATE kh_pack_question SET indexed=1 WHERE ns=? AND pack_id=? AND question_id=?", (ns, pack, qid))
    return True


def _dup_reply(ctx: Ctx, pack: str, r, score: float, match: str, threshold: float, project: str) -> dict:
    with sqldb.connect() as c:
        c.execute("UPDATE kh_pack_question SET asked_n=asked_n+1 WHERE ns=? AND pack_id=? AND question_id=?",
                  (ctx.ns, pack, r["question_id"]))
    out = {"duplicate_of": r["question_id"], "score": round(float(score), 6), "match": match, "threshold": threshold,
           "status": r["status"], "text": r["text"], "answer": r["answer"], "quote_id": r["quote_id"]}
    core.emit(ctx.ns, "kh.question_duplicate", {"pack": pack, "duplicate_of": r["question_id"],
                                                "score": out["score"], "match": match}, ctx, project=project)
    return out


def lookup(ctx: Ctx, pack: str, body: dict) -> tuple[int, dict]:
    text = _text(body.get("text"), "text", QUESTION_MAX, True)
    threshold = _threshold(ctx, body.get("threshold"))
    store = body.get("store", True)
    if not isinstance(store, bool):
        raise CapError("invalid", "store must be true or false")
    sha = text_sha(text)
    with sqldb.connect() as c:
        h = _visible_head(c, ctx, pack)
        project = h["project"] or ""
        ex = _q_by_sha(c, ctx.ns, pack, sha)
    if ex is not None and ex["status"] in ("open", "answered"):
        return 200, _dup_reply(ctx, pack, ex, 1.0, "exact", threshold, project)
    if ex is not None:  # an exact repeat of a closed question reopens it
        if not store:
            return 200, {"duplicate_of": None, "closed_match": ex["question_id"], "threshold": threshold,
                         "candidates": []}
        with sqldb.connect() as c:
            core.fence(ctx, c, required=_fenced(ctx))
            c.execute("UPDATE kh_pack_question SET status='open', asked_n=asked_n+1, updated_ms=?, rev=rev+1 "
                      "WHERE ns=? AND pack_id=? AND question_id=?", (sqldb.now_ms(c), ctx.ns, pack, ex["question_id"]))
            r = c.execute("SELECT * FROM kh_pack_question WHERE ns=? AND pack_id=? AND question_id=?",
                          (ctx.ns, pack, ex["question_id"])).fetchone()
        return 201, {**_q_view(pack, r), "duplicate_of": None, "reopened": True, "threshold": threshold,
                     "candidates": []}
    _best, cands, vec = _semantic(ctx, pack, text, threshold)
    rejected = []
    if cands:
        with sqldb.connect() as c:
            rows = {}
            for cd in cands:
                r = c.execute("SELECT * FROM kh_pack_question WHERE ns=? AND pack_id=? AND question_id=?",
                              (ctx.ns, pack, cd["question_id"])).fetchone()
                if r is not None and r["status"] in ("open", "answered"):
                    rows[cd["question_id"]] = r
        for cd in sorted(cands, key=lambda x: -x["score"]):
            r = rows.get(cd["question_id"])
            if r is None or cd["score"] < threshold:
                continue
            why = contrast(text, r["text"])
            if why is None:
                return 200, _dup_reply(ctx, pack, r, cd["score"], "semantic", threshold, project)
            rejected.append({"question_id": cd["question_id"], "score": round(float(cd["score"]), 6),
                             "contrast": why})
    if not store:
        return 200, {"duplicate_of": None, "threshold": threshold, "candidates": rejected}
    with sqldb.connect() as c:
        core.fence(ctx, c, required=_fenced(ctx))
        now = sqldb.now_ms(c)
        n = sqldb.counter_add(c, f"pq:{ctx.ns}:{pack}")
        qid = f"Q-{n}"
        ok = sqldb.insert_new(c, "kh_pack_question", {
            "ns": ctx.ns, "pack_id": pack, "question_id": qid, "n": n, "text": text, "text_sha": sha,
            "status": "open", "answer": None, "quote_id": None, "quote_ver": None, "asked_n": 1, "indexed": 0,
            "actor_json": ctx.actor_json(), "created_ms": now, "updated_ms": now, "rev": 1})
        if not ok:  # a concurrent identical question won the text_sha
            raced = _q_by_sha(c, ctx.ns, pack, sha)
    if not ok:
        return 200, _dup_reply(ctx, pack, raced, 1.0, "exact", threshold, project)
    if vec is not None:
        _index_question(ctx.ns, pack, qid, text, project, vec)
    with sqldb.connect() as c:
        r = c.execute("SELECT * FROM kh_pack_question WHERE ns=? AND pack_id=? AND question_id=?",
                      (ctx.ns, pack, qid)).fetchone()
    return 201, {**_q_view(pack, r), "duplicate_of": None, "threshold": threshold, "candidates": rejected}


def patch_question(ctx: Ctx, pack: str, qid: str, body: dict, expected: int | None) -> dict:
    status = body.get("status")
    if status is not None and status not in Q_STATUSES:
        raise CapError("invalid", f"status must be one of {', '.join(Q_STATUSES)}")
    answer = _text(body.get("answer"), "answer", TEXT_MAX, False)
    if answer is not None and status is None:
        status = "answered"
    with sqldb.connect() as c:
        core.fence(ctx, c, required=_fenced(ctx))
        h = _visible_head(c, ctx, pack)
        r = c.execute("SELECT * FROM kh_pack_question WHERE ns=? AND pack_id=? AND question_id=?",
                      (ctx.ns, pack, qid)).fetchone()
        if r is None:
            raise CapError("not_found", f"question '{qid}' not found")
        sets: dict = {"updated_ms": sqldb.now_ms(c), "actor_json": ctx.actor_json()}
        if status is not None:
            sets["status"] = status
        if answer is not None:
            sets["answer"] = answer
        if "quote_id" in body or "quote_ver" in body:
            sets["quote_id"], sets["quote_ver"] = _check_quote_ref(c, ctx, pack, h, body.get("quote_id"),
                                                                   body.get("quote_ver"))
        core.cas_update(c, "kh_pack_question", {"ns": ctx.ns, "pack_id": pack, "question_id": qid}, sets, expected,
                        ns=ctx.ns, what=f"question '{qid}'")
        return _q_view(pack, c.execute("SELECT * FROM kh_pack_question WHERE ns=? AND pack_id=? AND question_id=?",
                                       (ctx.ns, pack, qid)).fetchone())


def list_questions(ctx: Ctx, pack: str, status: str | None, limit: int) -> dict:
    with sqldb.connect() as c:
        _visible_head(c, ctx, pack)
        q, params = "SELECT * FROM kh_pack_question WHERE ns=? AND pack_id=?", [ctx.ns, pack]
        if status:
            st = core.csv(status)
            q += f" AND status IN ({','.join('?' for _ in st)})"
            params += st
        rows = c.execute(q + f" ORDER BY n LIMIT {int(limit)}", params).fetchall()
        return {"pack_id": pack, "questions": [_q_view(pack, r) for r in rows]}


def get_question(ctx: Ctx, pack: str, qid: str) -> dict:
    with sqldb.connect() as c:
        _visible_head(c, ctx, pack)
        r = c.execute("SELECT * FROM kh_pack_question WHERE ns=? AND pack_id=? AND question_id=?",
                      (ctx.ns, pack, qid)).fetchone()
        if r is None:
            raise CapError("not_found", f"question '{qid}' not found")
        return _q_view(pack, r)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


async def _body(request: Request, ctx: Ctx) -> dict:
    body = await core.read_object(request)
    return await core.run(core.apply_body_filters, ctx, "packs", body)


def attach(app: FastAPI) -> None:
    tag = ["v1 packs"]

    @app.get("/v1/{ns}/packs", tags=tag, summary="List packs")
    async def get_packs(ns: str, project: str = Query(""), after: str = Query(""), limit: str | None = Query(None),
                        ctx: Ctx = Depends(core.ns_ctx)):
        _require_read(ctx)
        return await core.run(list_packs, ctx, after, core.clamp_limit(limit, LIST_DEFAULT, LIST_MAX), project)

    @app.get("/v1/{ns}/packs/{pack}", tags=tag, summary="A pack version (?version=vN.k|latest&text=true)")
    async def get_pack_route(ns: str, pack: str, version: str | None = Query(None), text: bool = Query(False),
                             ctx: Ctx = Depends(core.ns_ctx)):
        _require_read(ctx)
        out = await core.run(get_pack, ctx, _check_pack(pack), version, text)
        return core.reply(out, etag_rev=out["version"]["rev"])

    @app.get("/v1/{ns}/packs/{pack}/versions", tags=tag, summary="List the versions of a pack")
    async def get_versions(ns: str, pack: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require_read(ctx)
        return await core.run(list_versions, ctx, _check_pack(pack))

    @app.post("/v1/{ns}/packs/{pack}/versions", tags=tag, summary="Create a new major version (draft)")
    @core.idempotent
    async def post_version(request: Request, ns: str, pack: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require_write(ctx)
        _check_pack(pack)
        body = await _body(request, ctx)
        out = await core.run(create_version, ctx, pack, body)
        if body.get("freeze"):
            out = await core.run(freeze, ctx, pack, out["label"], None)
        return core.reply(out, status=201, etag_rev=out["rev"])

    @app.post("/v1/{ns}/packs/{pack}/versions/{ver}/addenda", tags=tag, summary="Add an addendum to a frozen vN.k")
    @core.idempotent
    async def post_addendum(request: Request, ns: str, pack: str, ver: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require_write(ctx)
        _check_pack(pack)
        body = await _body(request, ctx)
        body.pop("base", None)
        out = await core.run(create_version, ctx, pack, body, ver)
        if body.get("freeze"):
            out = await core.run(freeze, ctx, pack, out["label"], None)
        return core.reply(out, status=201, etag_rev=out["rev"])

    @app.post("/v1/{ns}/packs/{pack}/versions/{ver}:freeze", tags=tag, summary="Freeze a draft version")
    @core.idempotent
    async def post_version_freeze(request: Request, ns: str, pack: str, ver: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require_write(ctx)
        _check_pack(pack)
        out = await core.run(freeze, ctx, pack, ver, _if_match(ctx, request))
        return core.reply(out, etag_rev=out["rev"])

    @app.post("/v1/{ns}/packs/{pack}/versions/{ver}:reindex", tags=tag, summary="Re-embed a frozen version")
    @core.idempotent
    async def post_version_reindex(request: Request, ns: str, pack: str, ver: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require_write(ctx)
        _check_pack(pack)
        out = await core.run(reindex, ctx, pack, ver)
        return core.reply(out, etag_rev=out["rev"])

    @app.patch("/v1/{ns}/packs/{pack}/versions/{ver}", tags=tag, summary="Edit a draft version")
    async def patch_version_route(request: Request, ns: str, pack: str, ver: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require_write(ctx)
        _check_pack(pack)
        expected = _if_match(ctx, request)
        body = await _body(request, ctx)
        out = await core.run(patch_version, ctx, pack, ver, body, expected)
        return core.reply(out, etag_rev=out["rev"])

    # -- quotes ---------------------------------------------------------------------------------------------
    @app.post("/v1/{ns}/packs/{pack}/quotes", tags=tag, summary="Cite a frozen version by offset/length or text")
    @core.idempotent
    async def post_quote(request: Request, ns: str, pack: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require_read(ctx)
        body = await _body(request, ctx)
        status, out = await core.run(cite, ctx, _check_pack(pack), body)
        return core.reply(out, status=status)

    @app.get("/v1/{ns}/packs/{pack}/quotes", tags=tag, summary="List quotes of a version")
    async def get_quotes(ns: str, pack: str, version: str | None = Query(None), path: str | None = Query(None),
                         origin: str = Query(""), after: str = Query(""), limit: str | None = Query(None),
                         ctx: Ctx = Depends(core.ns_ctx)):
        _require_read(ctx)
        return await core.run(list_quotes, ctx, _check_pack(pack), version, path, after,
                              core.clamp_limit(limit, LIST_DEFAULT, LIST_MAX), origin)

    @app.get("/v1/{ns}/quotes/{quote_id:path}", tags=tag, summary="Resolve a quote to the latest version")
    async def get_quote(ns: str, quote_id: str, pack: str = Query(""), version: str | None = Query(None),
                        ctx: Ctx = Depends(core.ns_ctx)):
        _require_read(ctx)
        return await core.run(resolve_quote, ctx, quote_id, _check_pack(pack) if pack else None, version)

    # -- assumptions ----------------------------------------------------------------------------------------
    @app.post("/v1/{ns}/packs/{pack}/assumptions", tags=tag, summary="Record an assumption (ASSUMED-n)")
    @core.idempotent
    async def post_assumption(request: Request, ns: str, pack: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require_write(ctx)
        body = await _body(request, ctx)
        out = await core.run(add_assumption, ctx, _check_pack(pack), body)
        return core.reply(out, status=201, etag_rev=out["rev"])

    @app.get("/v1/{ns}/packs/{pack}/assumptions", tags=tag, summary="List assumptions (?status=)")
    async def get_assumptions(ns: str, pack: str, status: str | None = Query(None), limit: str | None = Query(None),
                              ctx: Ctx = Depends(core.ns_ctx)):
        _require_read(ctx)
        return await core.run(list_assumptions, ctx, _check_pack(pack), status,
                              core.clamp_limit(limit, LIST_MAX, LIST_MAX))

    @app.get("/v1/{ns}/packs/{pack}/assumptions/{code}", tags=tag, summary="One assumption")
    async def get_assumption_route(ns: str, pack: str, code: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require_read(ctx)
        out = await core.run(get_assumption, ctx, _check_pack(pack), code)
        return core.reply(out, etag_rev=out["rev"])

    @app.patch("/v1/{ns}/packs/{pack}/assumptions/{code}", tags=tag, summary="Edit / confirm / refute / supersede")
    async def patch_assumption_route(request: Request, ns: str, pack: str, code: str,
                                     ctx: Ctx = Depends(core.ns_ctx)):
        _require_read(ctx)
        expected = _if_match(ctx, request)
        body = await _body(request, ctx)
        out = await core.run(patch_assumption, ctx, _check_pack(pack), code, body, expected)
        return core.reply(out, etag_rev=out["rev"])

    # -- questions ------------------------------------------------------------------------------------------
    @app.post("/v1/{ns}/packs/{pack}/questions:lookup", tags=tag, summary="Duplicate-question lookup (+ store)")
    @core.idempotent
    async def post_lookup(request: Request, ns: str, pack: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require_read(ctx)
        body = await _body(request, ctx)
        status, out = await core.run(lookup, ctx, _check_pack(pack), body)
        return core.reply(out, status=status)

    @app.get("/v1/{ns}/packs/{pack}/questions", tags=tag, summary="List questions (?status=)")
    async def get_questions(ns: str, pack: str, status: str | None = Query(None), limit: str | None = Query(None),
                            ctx: Ctx = Depends(core.ns_ctx)):
        _require_read(ctx)
        return await core.run(list_questions, ctx, _check_pack(pack), status,
                              core.clamp_limit(limit, LIST_MAX, LIST_MAX))

    @app.get("/v1/{ns}/packs/{pack}/questions/{qid}", tags=tag, summary="One question")
    async def get_question_route(ns: str, pack: str, qid: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require_read(ctx)
        out = await core.run(get_question, ctx, _check_pack(pack), qid)
        return core.reply(out, etag_rev=out["rev"])

    @app.patch("/v1/{ns}/packs/{pack}/questions/{qid}", tags=tag, summary="Answer / close / reopen a question")
    async def patch_question_route(request: Request, ns: str, pack: str, qid: str, ctx: Ctx = Depends(core.ns_ctx)):
        _require_write(ctx)
        expected = _if_match(ctx, request)
        body = await _body(request, ctx)
        out = await core.run(patch_question, ctx, _check_pack(pack), qid, body, expected)
        return core.reply(out, etag_rev=out["rev"])
