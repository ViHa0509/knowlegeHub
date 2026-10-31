"""redaction (spec §6.8, NAME `redaction`): secret/PII masking packs on every /v1 write path, scan/verify.

Packs are data (`app/caps/data/redact/<id>.v<ver>.json` for KH packs, stored under ns `_kh`; consumer packs under
their own ns). A pack = {patterns, vectors}; the vectors are conformance tests a consumer can replay. `scrub` is
registered as a core BODY_FILTER, so every module that runs `core.apply_body_filters` masks before validation and
storage. Vault canary scanners plug in through `register_canary(fn)` (see docs/integration-notes.md).
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, Request

from app import sqldb
from app.caps import core
from app.caps.core import CapError, Ctx

NAME = "redaction"
VERSION = "1.0.0"
STATUS = "beta"
DEFAULT_ON = True
MAX_PATTERNS = 100
MAX_REGEX = 512
MAX_VECTORS = 500
MAX_PACK_BYTES = 256 * 1024
MAX_SELECTED = 8
MAX_SCAN_BYTES = 256 * 1024
CACHE_S = 5.0
LIMITS = {"patterns_per_pack": MAX_PATTERNS, "regex_chars": MAX_REGEX, "vectors_per_pack": MAX_VECTORS,
          "pack_bytes": MAX_PACK_BYTES, "selected_packs": MAX_SELECTED, "scan_text_bytes": MAX_SCAN_BYTES}
TABLES = ["kh_redact_pack", "kh_redact_ns"]
LOCAL_TABLES: list[str] = []
DATA_DIR = Path(__file__).resolve().parent / "data" / "redact"
DEFAULT_PACK = "kh-default"
KH_PREFIX = "kh-"
CANARY = "canary"
PATTERN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
PACK_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
NESTED_QUANT_RE = re.compile(r"\(([^()]*)[+*}]([^()]*)\)[+*{]")
SPEC = {
    "matching": "all non-overlapping re.finditer matches per pattern (Python re semantics)",
    "entropy": "with min_entropy, keep only matches whose Shannon entropy (bits/char) >= min_entropy",
    "order": "sort findings by (start, -length, pattern order across the selected packs)",
    "merge": "overlapping spans merge into one; the label is the first finding's pattern name",
    "replace": "each merged span is replaced with [REDACTED:<name>]; canary hits always [REDACTED:canary]",
    "refuse": "any finding of a refuse-mode pattern refuses the write (422 redaction_refused); canaries never refuse",
    "clean": "masked text must scan clean",
}

_sel_cache: dict[str, tuple[float, "Scanner", dict]] = {}
_lock = threading.Lock()
_default: dict[str, Any] = {}
CANARY_SCANNERS: list[Callable[[str, str], list]] = []


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _m1(c) -> None:
    sqldb.create_table(
        c, "kh_redact_pack",
        "CREATE TABLE IF NOT EXISTS kh_redact_pack (ns TEXT NOT NULL, pack_id TEXT NOT NULL, version INTEGER NOT NULL, "
        "description TEXT, patterns_json TEXT, vectors_json TEXT, sha256 TEXT, created_ms INTEGER, actor_json TEXT, "
        "PRIMARY KEY (ns, pack_id, version))",
        "CREATE COLUMN TABLE kh_redact_pack (ns NVARCHAR(64) NOT NULL, pack_id NVARCHAR(64) NOT NULL, "
        "version BIGINT NOT NULL, description NVARCHAR(2000), patterns_json NCLOB, vectors_json NCLOB, "
        "sha256 NVARCHAR(64), created_ms BIGINT, actor_json NCLOB, PRIMARY KEY (ns, pack_id, version))")
    sqldb.create_table(
        c, "kh_redact_ns",
        "CREATE TABLE IF NOT EXISTS kh_redact_ns (ns TEXT PRIMARY KEY, packs_json TEXT, updated_ms INTEGER, "
        "actor_json TEXT, rev INTEGER NOT NULL DEFAULT 1)",
        "CREATE COLUMN TABLE kh_redact_ns (ns NVARCHAR(64) PRIMARY KEY, packs_json NCLOB, updated_ms BIGINT, "
        "actor_json NCLOB, rev BIGINT NOT NULL)")


def pack_sha(patterns: list, vectors: list) -> str:
    return core.sha256_json({"patterns": patterns, "vectors": vectors})


def file_packs() -> list[dict]:
    out = []
    if not DATA_DIR.is_dir():
        return out
    for f in sorted(DATA_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append(d)
    return out


def _seed(c) -> None:
    now = sqldb.now_ms(c)
    for d in file_packs():
        sqldb.insert_new(c, "kh_redact_pack", {
            "ns": core.KH_NS, "pack_id": d["id"], "version": int(d["version"]),
            "description": d.get("description") or "", "patterns_json": core.dumps(d["patterns"]),
            "vectors_json": core.dumps(d.get("vectors") or []), "sha256": pack_sha(d["patterns"], d.get("vectors") or []),
            "created_ms": now, "actor_json": core.dumps({"principal": "kh", "role": "admin"})})


def init(c) -> None:
    sqldb.migrate(c, NAME, [(1, _m1)])
    _seed(c)
    if _body_filter not in core.BODY_FILTERS:
        core.BODY_FILTERS.insert(0, _body_filter)
    invalidate()


def register_canary(fn: Callable[[str, str], list]) -> None:
    """Vault: fn(ns, text) -> [{start, end, vault, item, canary_kind, retired}]."""
    if fn not in CANARY_SCANNERS:
        CANARY_SCANNERS.append(fn)


def invalidate(ns: str | None = None) -> None:
    with _lock:
        if ns is None:
            _sel_cache.clear()
        else:
            _sel_cache.pop(ns, None)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum((k / n) * math.log2(k / n) for k in counts.values())


class Scanner:
    """Compiled patterns of the selected packs, in selection order."""

    def __init__(self, packs: list[dict]):
        self.refs = [p["ref"] for p in packs]
        self.patterns: list[tuple] = []  # (order, name, kind, mode, compiled, min_entropy, ref)
        for p in packs:
            for pat in p.get("patterns") or []:
                try:
                    rx = re.compile(pat["regex"])
                except (re.error, KeyError, TypeError):
                    continue
                self.patterns.append((len(self.patterns), pat["name"], pat.get("kind", "secret"),
                                      pat.get("mode", "mask"), rx, pat.get("min_entropy"), p["ref"]))

    def raw(self, text: str) -> list[dict]:
        found = []
        for order, name, kind, mode, rx, ment, ref in self.patterns:
            for m in rx.finditer(text):
                if m.end() <= m.start():
                    continue
                if ment and entropy(m.group(0)) < float(ment):
                    continue
                found.append({"name": name, "kind": kind, "mode": mode, "pack": ref, "start": m.start(),
                              "end": m.end(), "match": m.group(0), "_o": order})
        return found

    def scan(self, text: str, canaries: list | None = None) -> list[dict]:
        """Merged findings (sorted, overlaps folded into the first)."""
        found = self.raw(text or "")
        for h in canaries or []:
            found.append({"name": CANARY, "kind": "canary", "mode": "mask", "pack": "vault", "start": int(h["start"]),
                          "end": int(h["end"]), "match": text[int(h["start"]):int(h["end"])], "_o": -1,
                          "canary": h})
        found.sort(key=lambda f: (f["start"], -(f["end"] - f["start"]), f["_o"]))
        merged: list[dict] = []
        for f in found:
            if merged and f["start"] < merged[-1]["end"]:
                last = merged[-1]
                last["end"] = max(last["end"], f["end"])
                if f["mode"] == "refuse" and f["name"] != CANARY:
                    last["refuse_inside"] = True
                if f["name"] == CANARY and last["name"] != CANARY:
                    last["canary_inside"] = f.get("canary")
                continue
            merged.append(dict(f))
        for f in merged:
            f.pop("_o", None)
            f["match"] = text[f["start"]:f["end"]]
        return merged

    def mask(self, text: str, canaries: list | None = None) -> tuple[str, list[dict], bool]:
        found = self.scan(text, canaries)
        out, pos = [], 0
        for f in found:
            out.append(text[pos:f["start"]])
            label = CANARY if (f["name"] == CANARY or f.get("canary_inside")) else f["name"]
            out.append(f"[REDACTED:{label}]")
            pos = f["end"]
        out.append(text[pos:])
        refused = any((f["mode"] == "refuse" and f["name"] != CANARY) or f.get("refuse_inside") for f in found)
        return "".join(out), found, refused


def default_scanner() -> Scanner:
    """kh-default (latest version on disk), no DB. Used by the core distil leak gate."""
    sc = _default.get("scanner")
    if sc is None:
        packs = [d for d in file_packs() if d.get("id") == DEFAULT_PACK]
        packs.sort(key=lambda d: int(d.get("version") or 0))
        sel = [{"ref": f"{DEFAULT_PACK}@{packs[-1]['version']}", "patterns": packs[-1]["patterns"]}] if packs else []
        sc = Scanner(sel)
        _default["scanner"] = sc
    return sc


# ---------------------------------------------------------------------------
# Packs (DB)
# ---------------------------------------------------------------------------


def parse_ref(ref: Any) -> tuple[str, int | None]:
    if not isinstance(ref, str) or not ref.strip():
        raise CapError("invalid", "a pack reference is 'id' or 'id@version'")
    pid, sep, ver = ref.strip().partition("@")
    if not PACK_ID_RE.match(pid):
        raise CapError("invalid", f"pack id must match {PACK_ID_RE.pattern}")
    if not sep:
        return pid, None
    try:
        v = int(ver)
    except ValueError:
        raise CapError("invalid", "pack version must be an integer >= 1")
    if v < 1:
        raise CapError("invalid", "pack version must be an integer >= 1")
    return pid, v


def _pack_row(r, owner: str) -> dict:
    return {"ref": f"{r['pack_id']}@{int(r['version'])}", "id": r["pack_id"], "version": int(r["version"]),
            "owner": owner, "description": r["description"] or "", "patterns": core.loads(r["patterns_json"], []),
            "vectors": core.loads(r["vectors_json"], []), "sha256": r["sha256"], "created_ms": r["created_ms"]}


def resolve(c, ns: str, ref: str) -> dict | None:
    """A pack visible from ns (KH packs under _kh, else the ns's own)."""
    try:
        pid, ver = parse_ref(ref)
    except CapError:
        return None
    owner_ns = core.KH_NS if pid.startswith(KH_PREFIX) else ns
    if ver is None:
        r = c.execute("SELECT * FROM kh_redact_pack WHERE ns=? AND pack_id=? ORDER BY version DESC LIMIT 1",
                      (owner_ns, pid)).fetchone()
    else:
        r = c.execute("SELECT * FROM kh_redact_pack WHERE ns=? AND pack_id=? AND version=?",
                      (owner_ns, pid, ver)).fetchone()
    if r is None:
        return None
    return _pack_row(r, "kh" if owner_ns == core.KH_NS else ns)


def list_packs(c, ns: str | None) -> list[dict]:
    rows = c.execute("SELECT * FROM kh_redact_pack WHERE ns=? ORDER BY pack_id, version", (core.KH_NS,)).fetchall()
    out = [_pack_row(r, "kh") for r in rows]
    if ns:
        out += [_pack_row(r, ns) for r in c.execute(
            "SELECT * FROM kh_redact_pack WHERE ns=? ORDER BY pack_id, version", (ns,)).fetchall()]
    return out


def _summary(p: dict) -> dict:
    return {k: p[k] for k in ("ref", "id", "version", "owner", "description", "sha256", "created_ms")} | {
        "patterns": len(p["patterns"]), "vectors": len(p["vectors"])}


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def _selection(c, ns: str, st: core.NsState | None) -> dict:
    r = c.execute("SELECT packs_json, rev FROM kh_redact_ns WHERE ns=?", (ns,)).fetchone()
    if r is not None:
        return {"packs": core.loads(r[0], []) or [], "source": "api", "rev": int(r[1])}
    red = (st.config.get("redaction") if st else None) or {}
    packs = red.get("packs") if isinstance(red, dict) else None
    if isinstance(packs, list) and packs:
        return {"packs": [str(p) for p in packs], "source": "manifest", "rev": 0}
    return {"packs": [], "source": "default", "rev": 0}


def _effective(c, ns: str, refs: list[str]) -> tuple[list[dict], list[str]]:
    wanted = list(refs)
    if not any(str(x).partition("@")[0] == DEFAULT_PACK for x in wanted):
        wanted.insert(0, DEFAULT_PACK)
    packs, missing, seen = [], [], set()
    for ref in wanted:
        p = resolve(c, ns, ref)
        if p is None:
            missing.append(ref)
            continue
        if p["ref"] in seen:
            continue
        seen.add(p["ref"])
        packs.append(p)
    return packs, missing


def selection_view(ns: str, st: core.NsState | None) -> dict:
    with sqldb.connect() as c:
        sel = _selection(c, ns, st)
        packs, missing = _effective(c, ns, sel["packs"])
    return {**sel, "missing": missing,
            "effective": [{"ref": p["ref"], "owner": p["owner"], "sha256": p["sha256"]} for p in packs]}


def scanner_for(ns: str, st: core.NsState | None = None) -> tuple[Scanner, dict]:
    now = time.monotonic()
    hit = _sel_cache.get(ns)
    if hit is not None and now - hit[0] < CACHE_S:
        return hit[1], hit[2]
    st = st or core.ns_state(ns)
    with sqldb.connect() as c:
        sel = _selection(c, ns, st)
        packs, missing = _effective(c, ns, sel["packs"])
    sc = Scanner(packs)
    info = {"packs": [p["ref"] for p in packs], "missing": missing, "source": sel["source"]}
    with _lock:
        _sel_cache[ns] = (now, sc, info)
    return sc, info


# ---------------------------------------------------------------------------
# Scrub (BODY_FILTER)
# ---------------------------------------------------------------------------


def _ptr(parts: list) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts) or "/"


def _canary_hits(ns: str, text: str) -> list:
    hits = []
    for fn in CANARY_SCANNERS:
        try:
            hits.extend(fn(ns, text) or [])
        except Exception:  # noqa: BLE001
            continue
    return hits


def _enabled() -> bool:
    from app import caps

    return caps.module(NAME) is not None


def scrub(ctx: Ctx, where: str, body: Any, canaries: bool = True) -> Any:
    """Mask every string of a JSON value (keys untouched). Emits kh.redacted / kh.canary_hit; raises 422
    redaction_refused on a refuse-mode finding. Must run outside a transaction. No-op when the module is off."""
    if not _enabled() or ctx is None:
        return body
    sc, info = scanner_for(ctx.ns, ctx.st)
    findings: list[dict] = []
    canary_events: list[dict] = []
    refused = False
    use_canaries = canaries and bool(CANARY_SCANNERS) and not str(where).startswith("vault")

    def walk(v: Any, path: list) -> Any:
        nonlocal refused
        if isinstance(v, str):
            hits = _canary_hits(ctx.ns, v) if use_canaries else []
            masked, found, ref = sc.mask(v, hits)
            if not found:
                return v
            refused = refused or ref
            ptr = _ptr(path)
            for f in found:
                if f["name"] == CANARY or f.get("canary_inside"):
                    h = f.get("canary") or f.get("canary_inside") or {}
                    canary_events.append({"vault": h.get("vault"), "item": h.get("item"),
                                          "canary_kind": h.get("canary_kind"), "retired": bool(h.get("retired")),
                                          "principal": ctx.principal, "run_id": ctx.run_id,
                                          "unit_id": ctx.unit_id, "where": where, "pointer": ptr})
                    if f["name"] == CANARY:
                        continue
                findings.append({"name": f["name"], "kind": f["kind"], "mode": f["mode"], "pack": f["pack"],
                                 "pointer": ptr})
            return masked
        if isinstance(v, list):
            return [walk(x, path + [i]) for i, x in enumerate(v)]
        if isinstance(v, dict):
            return {k: walk(x, path + [k]) for k, x in v.items()}
        return v

    out = walk(body, [])
    for ev in canary_events:
        core.emit(ctx.ns, "kh.canary_hit", ev, ctx)
    if findings:
        core.emit(ctx.ns, "kh.redacted", {"where": where, "count": len(findings), "refused": refused,
                                          "packs": info["packs"], "findings": findings}, ctx)
    if refused:
        raise CapError("redaction_refused", "the body holds a value a refuse-mode redaction pattern forbids",
                       extra={"where": where, "findings": [f for f in findings if f["mode"] == "refuse"]})
    return out


def _body_filter(ctx: Ctx, where: str, body: Any) -> Any:
    return scrub(ctx, where, body)


# ---------------------------------------------------------------------------
# Pack validation and conformance
# ---------------------------------------------------------------------------


def check_patterns(patterns: Any) -> list[dict]:
    errs: list[dict] = []
    if not isinstance(patterns, list) or not patterns:
        return [{"pointer": "/patterns", "message": "patterns must be a non-empty list"}]
    if len(patterns) > MAX_PATTERNS:
        return [{"pointer": "/patterns", "message": f"at most {MAX_PATTERNS} patterns"}]
    names = set()
    for i, p in enumerate(patterns):
        ptr = f"/patterns/{i}"
        if not isinstance(p, dict):
            errs.append({"pointer": ptr, "message": "a pattern must be an object"})
            continue
        unknown = set(p) - {"name", "kind", "mode", "regex", "min_entropy", "description"}
        if unknown:
            errs.append({"pointer": ptr, "message": f"unknown keys: {', '.join(sorted(unknown))}"})
        name = p.get("name")
        if not isinstance(name, str) or not PATTERN_NAME_RE.match(name) or name == CANARY:
            errs.append({"pointer": f"{ptr}/name", "message": f"name must match {PATTERN_NAME_RE.pattern} "
                                                              "and not be 'canary'"})
        elif name in names:
            errs.append({"pointer": f"{ptr}/name", "message": f"duplicate pattern name '{name}'"})
        else:
            names.add(name)
        if p.get("kind", "secret") not in ("secret", "pii"):
            errs.append({"pointer": f"{ptr}/kind", "message": "kind must be secret or pii"})
        if p.get("mode", "mask") not in ("mask", "refuse"):
            errs.append({"pointer": f"{ptr}/mode", "message": "mode must be mask or refuse"})
        me = p.get("min_entropy")
        if me is not None and (isinstance(me, bool) or not isinstance(me, (int, float)) or not 0 < me <= 8):
            errs.append({"pointer": f"{ptr}/min_entropy", "message": "min_entropy must be in (0, 8]"})
        rx = p.get("regex")
        if not isinstance(rx, str) or not rx or len(rx) > MAX_REGEX:
            errs.append({"pointer": f"{ptr}/regex", "message": f"regex must be 1-{MAX_REGEX} characters"})
            continue
        try:
            comp = re.compile(rx)
        except re.error as e:
            errs.append({"pointer": f"{ptr}/regex", "message": f"regex does not compile: {e}"})
            continue
        if comp.search("") is not None:
            errs.append({"pointer": f"{ptr}/regex", "message": "regex must not match the empty string"})
        if NESTED_QUANT_RE.search(rx):
            errs.append({"pointer": f"{ptr}/regex", "message": "nested quantifiers are refused"})
    return errs


def check_vectors(vectors: Any, names: set[str]) -> list[dict]:
    if not isinstance(vectors, list) or not vectors:
        return [{"pointer": "/vectors", "message": "vectors must be a non-empty list"}]
    if len(vectors) > MAX_VECTORS:
        return [{"pointer": "/vectors", "message": f"at most {MAX_VECTORS} vectors"}]
    errs: list[dict] = []
    used: set[str] = set()
    for i, v in enumerate(vectors):
        ptr = f"/vectors/{i}"
        if not isinstance(v, dict) or not isinstance(v.get("text"), str):
            errs.append({"pointer": ptr, "message": "a vector is {text, findings:[names], masked?, refused?}"})
            continue
        f = v.get("findings")
        if not isinstance(f, list) or not all(isinstance(x, str) for x in f):
            errs.append({"pointer": f"{ptr}/findings", "message": "findings must be a list of pattern names"})
            continue
        bad = [x for x in f if x not in names]
        if bad:
            errs.append({"pointer": f"{ptr}/findings", "message": f"unknown pattern names: {', '.join(bad)}"})
        used |= set(f)
        if "masked" in v and not isinstance(v["masked"], str):
            errs.append({"pointer": f"{ptr}/masked", "message": "masked must be a string"})
        if "refused" in v and not isinstance(v["refused"], bool):
            errs.append({"pointer": f"{ptr}/refused", "message": "refused must be a boolean"})
    for n in sorted(names - used):
        errs.append({"pointer": "/vectors", "message": f"pattern '{n}' is not exercised by any vector"})
    return errs


def conformance(pack: dict) -> dict:
    """Run a pack's own vectors against the pack alone."""
    sc = Scanner([{"ref": pack.get("ref") or "pack", "patterns": pack.get("patterns") or []}])
    failures = []
    vectors = pack.get("vectors") or []
    for i, v in enumerate(vectors):
        masked, found, refused = sc.mask(v.get("text") or "")
        got = [f["name"] for f in found]
        why = []
        if got != list(v.get("findings") or []):
            why.append(f"findings {got} != {v.get('findings')}")
        if "masked" in v and masked != v["masked"]:
            why.append("masked text differs")
        if "refused" in v and refused != v["refused"]:
            why.append(f"refused {refused} != {v['refused']}")
        if sc.scan(masked):
            why.append("masked text does not scan clean")
        if why:
            failures.append({"index": i, "reason": "; ".join(why), "got": {"findings": got, "masked": masked,
                                                                          "refused": refused}})
    return {"vectors": len(vectors), "passed": len(vectors) - len(failures), "failures": failures}


def put_pack(ctx: Ctx, ref: str, body: dict) -> tuple[int, dict]:
    pid, ver = parse_ref(ref)
    if ver is None:
        raise CapError("invalid", "name the version: PUT .../redaction/packs/{id}@{version}")
    if pid.startswith(KH_PREFIX):
        raise CapError("invalid", f"pack ids starting '{KH_PREFIX}' are reserved for KH")
    core.check_size(body, MAX_PACK_BYTES, "pack")
    desc = body.get("description") or ""
    if not isinstance(desc, str) or len(desc) > 2000:
        raise CapError("invalid", "description must be a string of at most 2000 characters")
    patterns, vectors = body.get("patterns"), body.get("vectors")
    errs = check_patterns(patterns)
    if not errs:
        errs = check_vectors(vectors, {p["name"] for p in patterns})
    if errs:
        raise CapError("invalid", "the pack is not valid", extra={"errors": errs[:core.MAX_SCHEMA_ERRORS]})
    conf = conformance({"ref": f"{pid}@{ver}", "patterns": patterns, "vectors": vectors})
    if conf["failures"]:
        raise CapError("invalid", "the pack fails its own vectors", extra={"conformance": conf})
    sha = pack_sha(patterns, vectors)
    with sqldb.connect() as c:
        r = c.execute("SELECT sha256 FROM kh_redact_pack WHERE ns=? AND pack_id=? AND version=?",
                      (ctx.ns, pid, ver)).fetchone()
        if r is not None:
            if r[0] != sha:
                raise CapError("pack_immutable", f"pack {pid}@{ver} exists with other content; publish a new "
                                                 "version", extra={"ref": f"{pid}@{ver}", "sha256": r[0]})
            return 200, {"ref": f"{pid}@{ver}", "sha256": sha, "created": False, "conformance": conf}
        sqldb.insert_new(c, "kh_redact_pack", {
            "ns": ctx.ns, "pack_id": pid, "version": ver, "description": desc,
            "patterns_json": core.dumps(patterns), "vectors_json": core.dumps(vectors), "sha256": sha,
            "created_ms": sqldb.now_ms(c), "actor_json": ctx.actor_json()})
    invalidate(ctx.ns)
    return 201, {"ref": f"{pid}@{ver}", "sha256": sha, "created": True, "conformance": conf}


def put_selection(ctx: Ctx, packs: Any, expected: int | None) -> dict:
    if not isinstance(packs, list) or not all(isinstance(p, str) for p in packs):
        raise CapError("invalid", "packs must be a list of pack references ('id' or 'id@version')")
    if len(packs) > MAX_SELECTED:
        raise CapError("invalid", f"at most {MAX_SELECTED} packs may be selected")
    for p in packs:
        parse_ref(p)
    with sqldb.connect() as c:
        now = sqldb.now_ms(c)
        r = c.execute("SELECT rev FROM kh_redact_ns WHERE ns=?", (ctx.ns,)).fetchone()
        if r is None:
            if expected is not None and expected > 0:
                raise core.write_conflict(ctx.ns, "redaction selection", None, expected)
            sqldb.insert_new(c, "kh_redact_ns", {"ns": ctx.ns, "packs_json": core.dumps(packs), "updated_ms": now,
                                                 "actor_json": ctx.actor_json(), "rev": 1})
        else:
            core.cas_update(c, "kh_redact_ns", {"ns": ctx.ns},
                            {"packs_json": core.dumps(packs), "updated_ms": now, "actor_json": ctx.actor_json()},
                            expected, ns=ctx.ns, what="redaction selection")
    invalidate(ctx.ns)
    return selection_view(ctx.ns, ctx.st)


def delete_selection(ctx: Ctx, expected: int | None) -> dict:
    with sqldb.connect() as c:
        r = c.execute("SELECT rev FROM kh_redact_ns WHERE ns=?", (ctx.ns,)).fetchone()
        if r is not None:
            if expected is not None and expected >= 0 and int(r[0]) != expected:
                raise core.write_conflict(ctx.ns, "redaction selection", int(r[0]), expected)
            c.execute("DELETE FROM kh_redact_ns WHERE ns=?", (ctx.ns,))
    invalidate(ctx.ns)
    return selection_view(ctx.ns, ctx.st)


def scan_text(ctx: Ctx, text: Any) -> dict:
    if not isinstance(text, str):
        raise CapError("invalid", "text must be a string")
    if len(text.encode("utf-8")) > MAX_SCAN_BYTES:
        raise CapError("too_large", f"text is larger than {MAX_SCAN_BYTES} bytes", extra={"limit": MAX_SCAN_BYTES})
    sc, info = scanner_for(ctx.ns, ctx.st)
    masked, found, refused = sc.mask(text)  # no canary scan: not a canary oracle
    return {"masked": masked, "refused": refused, "packs": info["packs"],
            "findings": [{"name": f["name"], "kind": f["kind"], "mode": f["mode"], "pack": f["pack"],
                          "start": f["start"], "end": f["end"]} for f in found]}


def verify(ctx: Ctx) -> dict:
    with sqldb.connect() as c:
        sel = _selection(c, ctx.ns, ctx.st)
        packs, missing = _effective(c, ctx.ns, sel["packs"])
    out = []
    for p in packs:
        conf = conformance(p)
        out.append({"ref": p["ref"], "sha256": p["sha256"], **conf})
    return {"packs": out, "missing": missing, "all_passed": not missing and all(not p["failures"] for p in out)}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _signed_in() -> None:
    from app import auth

    if auth.enabled() and auth.current() is None:
        raise CapError("unauthorized", "sign-in required")


def _kh_packs(ref: str | None) -> Any:
    with sqldb.connect() as c:
        if ref is None:
            return {"packs": [_summary(p) for p in list_packs(c, None)], "spec": SPEC}
        pid, _ = parse_ref(ref)
        p = resolve(c, core.KH_NS, ref) if pid.startswith(KH_PREFIX) else None
    if p is None:
        raise CapError("not_found", f"pack '{ref}' not found")
    return {**p, "spec": SPEC}


def _ns_packs(ctx: Ctx, ref: str | None) -> Any:
    with sqldb.connect() as c:
        if ref is None:
            return {"packs": [_summary(p) for p in list_packs(c, ctx.ns)], "spec": SPEC}
        p = resolve(c, ctx.ns, ref)
    if p is None:
        raise CapError("not_found", f"pack '{ref}' not found")
    return {**p, "spec": SPEC}


def attach(app: FastAPI) -> None:
    tag = ["v1 redaction"]

    @app.get("/v1/redaction/packs", tags=tag, summary="KH redaction packs")
    async def kh_packs():
        _signed_in()
        return await core.run(_kh_packs, None)

    @app.get("/v1/redaction/packs/{ref}", tags=tag, summary="One KH redaction pack")
    async def kh_pack(ref: str):
        _signed_in()
        return await core.run(_kh_packs, ref)

    @app.post("/v1/{ns}/redaction:scan", tags=tag, summary="Scan and mask a text with the ns packs")
    async def post_scan(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        body = await core.read_object(request)
        return core.reply(await core.run(scan_text, ctx, body.get("text")))

    @app.post("/v1/{ns}/redaction:verify", tags=tag, summary="Run every effective pack's conformance vectors")
    async def post_verify(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        return core.reply(await core.run(verify, ctx))

    @app.get("/v1/{ns}/redaction", tags=tag, summary="Redaction pack selection of the namespace")
    async def get_selection(ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        out = await core.run(selection_view, ctx.ns, ctx.st)
        return core.reply(out, etag_rev=out["rev"])

    @app.put("/v1/{ns}/redaction", tags=tag, summary="Select redaction packs (API selection)")
    async def put_sel(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require_admin()
        body = await core.read_object(request)
        out = await core.run(put_selection, ctx, body.get("packs"), core.if_match(request))
        return core.reply(out, etag_rev=out["rev"])

    @app.delete("/v1/{ns}/redaction", tags=tag, summary="Drop the API selection (manifest or default applies)")
    async def del_sel(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require_admin()
        out = await core.run(delete_selection, ctx, core.if_match(request))
        return core.reply(out)

    @app.get("/v1/{ns}/redaction/packs", tags=tag, summary="KH and own redaction packs")
    async def ns_packs(ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        return await core.run(_ns_packs, ctx, None)

    @app.get("/v1/{ns}/redaction/packs/{ref}", tags=tag, summary="One redaction pack")
    async def ns_pack(ns: str, ref: str, ctx: Ctx = Depends(core.ns_ctx)):
        return await core.run(_ns_packs, ctx, ref)

    @app.put("/v1/{ns}/redaction/packs/{ref}", tags=tag, summary="Publish an own pack id@version (immutable)")
    async def put_ns_pack(request: Request, ns: str, ref: str, ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require_admin()
        body = await core.read_object(request)
        status, out = await core.run(put_pack, ctx, ref, body)
        return core.reply(out, status=status)
