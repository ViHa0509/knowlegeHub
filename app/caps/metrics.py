"""metrics (spec §6.13): outcome metric observations, daily rollup, aggregate, A/B compare and the automatic
item rollback (D12) that joins a guard metric to the units a `kh.served` event served an item to.

Series = manifest types with `kind_of: metric` (schema validates `dims`, ACLs, fencing, retention_days prunes raw
rows; the rollup is kept). Settings: `settings.metrics {series:{<name>:{scale binary|real, dir up|down}},
rollback:{enabled, min_n, min_rel, alpha, window_days, control not_served|pre_activation, test}}`.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Query, Request

from app import config, sqldb
from app.caps import core
from app.caps.core import CapError, Ctx

NAME = "metrics"
VERSION = "1.0.0"
STATUS = "beta"
DEFAULT_ON = True
BATCH_MAX = 1000
RAW_MAX = 200_000
SERVED_SCAN_MAX = 500_000
LIST_MAX = 1000
DIMS_KEYS_MAX = 16
DIMS_BYTES_MAX = 4096
DAY_MS = 86_400_000
LIMITS = {"batch": BATCH_MAX, "raw_rows": RAW_MAX, "served_scan": SERVED_SCAN_MAX, "list": LIST_MAX,
          "dims_keys": DIMS_KEYS_MAX, "dims_bytes": DIMS_BYTES_MAX}
TABLES = ["kh_metric", "kh_metric_day"]
LOCAL_TABLES: list[str] = []

ROLLBACK_DEFAULTS = {"enabled": True, "min_n": 30, "min_rel": 0.10, "alpha": 0.05, "window_days": 14,
                     "control": "not_served", "test": "auto"}
OBS_KEYS = {"series", "value", "ts_ms", "unit_id", "run_id", "project", "dims", "baseline"}
GROUP_BY = ("day", "week", "project", "baseline", "run_id", "unit_id")
ROLLBACK_LEASE = "metrics_rollback"
COLS = ("ns", "series", "ts_ms", "unit_id", "project", "val", "run_id", "dims_json", "is_baseline", "actor_json",
        "received_ms")


def rollback_interval_s() -> int:
    return config._int("KH_METRICS_ROLLBACK_S", 21600)


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _m1(c) -> None:
    sqldb.create_table(
        c, "kh_metric",
        "CREATE TABLE IF NOT EXISTS kh_metric (ns TEXT NOT NULL, series TEXT NOT NULL, ts_ms INTEGER NOT NULL, "
        "unit_id TEXT NOT NULL DEFAULT '', project TEXT NOT NULL DEFAULT '', val REAL NOT NULL, run_id TEXT, "
        "dims_json TEXT, is_baseline INTEGER NOT NULL DEFAULT 0, actor_json TEXT, received_ms INTEGER, "
        "PRIMARY KEY (ns, series, ts_ms, unit_id))",
        "CREATE COLUMN TABLE kh_metric (ns NVARCHAR(64) NOT NULL, series NVARCHAR(128) NOT NULL, "
        "ts_ms BIGINT NOT NULL, unit_id NVARCHAR(256) NOT NULL, project NVARCHAR(256) NOT NULL, val DOUBLE NOT NULL, "
        "run_id NVARCHAR(256), dims_json NCLOB, is_baseline INTEGER NOT NULL, actor_json NCLOB, received_ms BIGINT, "
        "PRIMARY KEY (ns, series, ts_ms, unit_id))")
    sqldb.create_index(c, "kh_metric_run", "kh_metric", ["ns", "series", "run_id"])
    sqldb.create_index(c, "kh_metric_unit", "kh_metric", ["ns", "series", "unit_id"])
    sqldb.create_table(
        c, "kh_metric_day",
        "CREATE TABLE IF NOT EXISTS kh_metric_day (ns TEXT NOT NULL, project TEXT NOT NULL, series TEXT NOT NULL, "
        "day_no INTEGER NOT NULL, is_baseline INTEGER NOT NULL, n INTEGER NOT NULL DEFAULT 0, "
        "vsum REAL NOT NULL DEFAULT 0, vsumsq REAL NOT NULL DEFAULT 0, "
        "PRIMARY KEY (ns, project, series, day_no, is_baseline))",
        "CREATE COLUMN TABLE kh_metric_day (ns NVARCHAR(64) NOT NULL, project NVARCHAR(256) NOT NULL, "
        "series NVARCHAR(128) NOT NULL, day_no BIGINT NOT NULL, is_baseline INTEGER NOT NULL, n BIGINT NOT NULL, "
        "vsum DOUBLE NOT NULL, vsumsq DOUBLE NOT NULL, PRIMARY KEY (ns, project, series, day_no, is_baseline))")


def _usage(c, ns: str, type_: str) -> int:
    if not sqldb.table_exists(c, "kh_metric"):
        return 0
    return int(c.execute("SELECT COUNT(*) FROM kh_metric WHERE ns=? AND series=?", (ns, type_)).fetchone()[0])


def init(c) -> None:
    sqldb.migrate(c, NAME, [(1, _m1)])
    if _usage not in core.TYPE_USAGE:
        core.TYPE_USAGE.append(_usage)


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


def series_conf(st: core.NsState, series: str) -> dict:
    s = (st.settings("metrics").get("series") or {}).get(series) or {}
    return s if isinstance(s, dict) else {}


def rollback_conf(st: core.NsState) -> dict:
    out = dict(ROLLBACK_DEFAULTS)
    r = st.settings("metrics").get("rollback") or {}
    if isinstance(r, dict):
        out.update({k: v for k, v in r.items() if k in ROLLBACK_DEFAULTS and v is not None})
    return out


def _metric_type(ctx: Ctx, series: Any, op: str) -> dict:
    if not isinstance(series, str) or not series:
        raise CapError("invalid", "series is required")
    trow = ctx.type(series)
    if trow is None or trow.get("kind_of") != "metric":
        raise CapError("not_found", f"'{series}' is not a metric series of the manifest")
    if not ctx.type_may(trow, op):
        raise CapError("forbidden", f"role '{ctx.role}' may not {op} '{series}'")
    return trow


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def _check_obs(ctx: Ctx, i: int, ob: Any) -> tuple[dict | None, list[dict], dict | None]:
    errs: list[dict] = []
    if not isinstance(ob, dict):
        return None, [{"pointer": "/", "message": "an observation must be an object"}], None
    for k in ob:
        if k not in OBS_KEYS:
            errs.append({"pointer": f"/{k}", "message": "unknown key"})
    series = ob.get("series")
    trow = None
    try:
        trow = _metric_type(ctx, series, "write")
    except CapError as e:
        errs.append({"pointer": "/series", "message": str(e.detail)})
    val = ob.get("value")
    if not _num(val):
        errs.append({"pointer": "/value", "message": "value must be a finite number"})
    elif trow is not None and series_conf(ctx.st, series).get("scale") == "binary" and float(val) not in (0.0, 1.0):
        errs.append({"pointer": "/value", "message": f"'{series}' is binary: value must be 0 or 1"})
    ts = ob.get("ts_ms")
    if ts is None:
        ts = core.now_ms()
    elif not isinstance(ts, int) or isinstance(ts, bool) or ts < 0:
        errs.append({"pointer": "/ts_ms", "message": "ts_ms must be epoch milliseconds"})
    unit = ob.get("unit_id", ctx.unit_id)
    run = ob.get("run_id", ctx.run_id)
    for k, v in (("unit_id", unit), ("run_id", run)):
        if v is not None and (not isinstance(v, str) or len(v) > 256):
            errs.append({"pointer": f"/{k}", "message": f"{k} must be a string (<= 256)"})
    project = ob.get("project")
    if project is not None and not isinstance(project, str):
        errs.append({"pointer": "/project", "message": "project must be a string"})
        project = None
    try:
        project = ctx.require_project(project)
    except CapError as e:
        errs.append({"pointer": "/project", "message": str(e.detail)})
    dims = ob.get("dims") or {}
    if not isinstance(dims, dict):
        errs.append({"pointer": "/dims", "message": "dims must be an object"})
        dims = {}
    elif len(dims) > DIMS_KEYS_MAX:
        errs.append({"pointer": "/dims", "message": f"at most {DIMS_KEYS_MAX} dims"})
    elif core.json_size(dims) > DIMS_BYTES_MAX:
        errs.append({"pointer": "/dims", "message": f"dims are larger than {DIMS_BYTES_MAX} bytes"})
    elif trow is not None:
        for e in core.validate(ctx.st.schema_for(trow), dims):
            errs.append({"pointer": "/dims" + (e["pointer"] if e["pointer"] != "/" else ""),
                         "message": e["message"]})
    base = ob.get("baseline", False)
    if not isinstance(base, bool):
        errs.append({"pointer": "/baseline", "message": "baseline must be true or false"})
    if errs:
        return None, errs[:core.MAX_SCHEMA_ERRORS], trow
    row = {"ns": ctx.ns, "series": series, "ts_ms": int(ts), "unit_id": unit or "", "project": project or "",
           "val": float(val), "run_id": run, "dims_json": core.canonical(dims) if dims else None,
           "is_baseline": 1 if base else 0, "actor_json": ctx.actor_json()}
    return row, [], trow


def _rollup_add(c, row: dict) -> None:
    keys = (row["ns"], row["project"], row["series"], int(row["ts_ms"]) // DAY_MS, row["is_baseline"])
    v = float(row["val"])
    cur = c.execute("UPDATE kh_metric_day SET n=n+1, vsum=vsum+?, vsumsq=vsumsq+? WHERE ns=? AND project=? "
                    "AND series=? AND day_no=? AND is_baseline=?", (v, v * v, *keys))
    if not cur.rowcount or cur.rowcount <= 0:
        if not sqldb.insert_new(c, "kh_metric_day", {"ns": keys[0], "project": keys[1], "series": keys[2],
                                                     "day_no": keys[3], "is_baseline": keys[4], "n": 1,
                                                     "vsum": v, "vsumsq": v * v}):
            c.execute("UPDATE kh_metric_day SET n=n+1, vsum=vsum+?, vsumsq=vsumsq+? WHERE ns=? AND project=? "
                      "AND series=? AND day_no=? AND is_baseline=?", (v, v * v, *keys))


def write(ctx: Ctx, observations: list) -> dict:
    if not observations:
        raise CapError("invalid", "no observations given")
    if len(observations) > BATCH_MAX:
        raise CapError("too_large", f"at most {BATCH_MAX} observations per call", extra={"limit": BATCH_MAX})
    rows, rejected, fenced = [], [], False
    for i, ob in enumerate(observations):
        if isinstance(ob, dict) and ob.get("dims"):
            try:
                ob = {**ob, "dims": core.apply_body_filters(ctx, "metrics", ob.get("dims"))}
            except CapError as e:
                rejected.append({"index": i, "errors": [{"pointer": "/dims", "message": f"{e.code}: {e.detail}"}]})
                continue
        row, errs, trow = _check_obs(ctx, i, ob)
        if errs:
            rejected.append({"index": i, "errors": errs})
            continue
        fenced = fenced or bool(trow and trow.get("fenced"))
        rows.append((i, row))
    if rejected:
        raise CapError("validation_failed", "some observations are not valid",
                       extra={"accepted": [], "rejected": rejected})
    core.fence_required(ctx, fenced)
    accepted, created, dups, conflicts = [], 0, 0, 0
    with sqldb.connect() as c:
        core.fence(ctx, c, required=fenced)
        now = sqldb.now_ms(c)
        for i, row in rows:
            row["received_ms"] = now
            out = {"index": i, "series": row["series"], "ts_ms": row["ts_ms"], "unit_id": row["unit_id"]}
            if sqldb.insert_new(c, "kh_metric", row):
                _rollup_add(c, row)
                out["status"] = "created"
                created += 1
            else:
                r = c.execute("SELECT val FROM kh_metric WHERE ns=? AND series=? AND ts_ms=? AND unit_id=?",
                              (row["ns"], row["series"], row["ts_ms"], row["unit_id"])).fetchone()
                if r is not None and float(r[0]) == float(row["val"]):
                    out["status"] = "duplicate"
                    dups += 1
                else:
                    out["status"] = "conflict"
                    out["stored_value"] = float(r[0]) if r is not None else None
                    conflicts += 1
            accepted.append(out)
    if dups or conflicts:
        core.emit(ctx.ns, "kh.duplicate", {"what": "metric", "duplicates": dups, "conflicts": conflicts}, ctx)
    return {"accepted": accepted, "created": created, "duplicates": dups, "conflicts": conflicts}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _decode(r) -> dict:
    return {"series": r["series"], "ts_ms": int(r["ts_ms"]), "unit_id": r["unit_id"] or None,
            "project": r["project"], "value": float(r["val"]), "run_id": r["run_id"],
            "dims": core.loads(r["dims_json"], {}) or {}, "baseline": bool(r["is_baseline"]),
            "actor": core.loads(r["actor_json"], {}), "received_ms": r["received_ms"]}


def _project_clause(ctx: Ctx, project: str | None, where: list, params: list) -> None:
    if project:
        if not ctx.may_project(project):
            raise CapError("forbidden", f"no grant on project '{project}'")
        where.append("project=?")
        params.append(project)
        return
    eff = ctx.projects()
    if "*" in eff:
        return
    vis = [""] + eff
    where.append(f"project IN ({', '.join('?' for _ in vis)})")
    params.extend(vis)


def _bool_param(v: Any, name: str) -> bool | None:
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    raise CapError("invalid", f"{name} must be true or false")


def list_obs(ctx: Ctx, series: str | None, project: str | None, run_id: str | None, unit_id: str | None,
             baseline: bool | None, from_ms: int | None, to_ms: int | None, limit: int, offset: int) -> list[dict]:
    where, params = ["ns=?"], [ctx.ns]
    if series:
        _metric_type(ctx, series, "read")
        where.append("series=?")
        params.append(series)
    else:
        hidden = [t for t, row in ctx.st.types.items() if row.get("kind_of") == "metric"
                  and not ctx.type_may(row, "read")]
        if hidden:
            where.append(f"series NOT IN ({', '.join('?' for _ in hidden)})")
            params += hidden
    _project_clause(ctx, project, where, params)
    for col, v in (("run_id", run_id), ("unit_id", unit_id)):
        if v:
            where.append(f"{col}=?")
            params.append(v)
    if baseline is not None:
        where.append("is_baseline=?")
        params.append(1 if baseline else 0)
    if from_ms is not None:
        where.append("ts_ms>=?")
        params.append(int(from_ms))
    if to_ms is not None:
        where.append("ts_ms<?")
        params.append(int(to_ms))
    with sqldb.connect() as c:
        rows = c.execute(f"SELECT * FROM kh_metric WHERE {' AND '.join(where)} ORDER BY ts_ms DESC, series, unit_id "
                         f"LIMIT {int(limit)} OFFSET {int(offset)}", params).fetchall()
        return [_decode(r) for r in rows]


def _stats(vals: list[float]) -> dict:
    n = len(vals)
    s = sum(vals)
    return _stats_from(n, s, sum(v * v for v in vals))


def _stats_from(n: int, s: float, ss: float) -> dict:
    mean = s / n if n else None
    sd = None
    if n > 1:
        var = max(0.0, (ss - s * s / n) / (n - 1))
        sd = math.sqrt(var)
    elif n == 1:
        sd = 0.0
    return {"n": n, "sum": s, "mean": mean, "sd": sd}


def day_key(day_no: int) -> str:
    return datetime.fromtimestamp(day_no * 86400, tz=timezone.utc).strftime("%Y-%m-%d")


def week_key(ts_ms: int) -> str:
    y, w, _ = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isocalendar()
    return f"{y}-W{w:02d}"


def parse_window(v: Any) -> int | None:
    """`7d`, `12h`, `30m`, or plain days -> ms."""
    if v is None or v == "":
        return None
    s = str(v).strip().lower()
    mult = {"d": DAY_MS, "h": 3_600_000, "m": 60_000, "w": 7 * DAY_MS}
    try:
        if s[-1] in mult:
            return int(float(s[:-1]) * mult[s[-1]])
        return int(float(s) * DAY_MS)
    except (ValueError, IndexError):
        raise CapError("invalid", "window must look like 7d, 12h, 30m or 2w")


def aggregate(ctx: Ctx, series: str, group_by: str, from_ms: int | None, to_ms: int | None,
              project: str | None, baseline: bool | None) -> dict:
    _metric_type(ctx, series, "read")
    gb = group_by or "day"
    dim = None
    if gb.startswith("dims."):
        dim = gb[5:]
        if not dim:
            raise CapError("invalid", "group_by dims.<name> needs a name")
    elif gb not in GROUP_BY:
        raise CapError("invalid", f"group_by must be one of {', '.join(GROUP_BY)} or dims.<name>")
    aligned = all(v is None or v % DAY_MS == 0 for v in (from_ms, to_ms))
    use_rollup = gb in ("day", "week", "project", "baseline") and aligned
    with sqldb.connect() as c:
        if use_rollup:
            where, params = ["ns=?", "series=?"], [ctx.ns, series]
            _project_clause(ctx, project, where, params)
            if baseline is not None:
                where.append("is_baseline=?")
                params.append(1 if baseline else 0)
            if from_ms is not None:
                where.append("day_no>=?")
                params.append(from_ms // DAY_MS)
            if to_ms is not None:
                where.append("day_no<?")
                params.append(to_ms // DAY_MS)
            acc: dict[Any, list] = {}
            for r in c.execute(f"SELECT project, day_no, is_baseline, n, vsum, vsumsq FROM kh_metric_day "
                               f"WHERE {' AND '.join(where)}", params).fetchall():
                if gb == "day":
                    k = day_key(int(r[1]))
                elif gb == "week":
                    k = week_key(int(r[1]) * DAY_MS)
                elif gb == "project":
                    k = r[0]
                else:
                    k = bool(r[2])
                a = acc.setdefault(k, [0, 0.0, 0.0])
                a[0] += int(r[3])
                a[1] += float(r[4])
                a[2] += float(r[5])
            groups = [{"key": k, **_stats_from(a[0], a[1], a[2])} for k, a in acc.items()]
            source, truncated = "rollup", False
        else:
            where, params = ["ns=?", "series=?"], [ctx.ns, series]
            _project_clause(ctx, project, where, params)
            if baseline is not None:
                where.append("is_baseline=?")
                params.append(1 if baseline else 0)
            if from_ms is not None:
                where.append("ts_ms>=?")
                params.append(from_ms)
            if to_ms is not None:
                where.append("ts_ms<?")
                params.append(to_ms)
            rows = c.execute(f"SELECT ts_ms, project, is_baseline, run_id, unit_id, dims_json, val FROM kh_metric "
                             f"WHERE {' AND '.join(where)} ORDER BY ts_ms LIMIT {RAW_MAX + 1}", params).fetchall()
            truncated = len(rows) > RAW_MAX
            rows = rows[:RAW_MAX]
            vals: dict[Any, list] = {}
            for r in rows:
                if dim is not None:
                    k = (core.loads(r[5], {}) or {}).get(dim)
                    if isinstance(k, (dict, list)):
                        k = core.canonical(k)
                elif gb == "day":
                    k = day_key(int(r[0]) // DAY_MS)
                elif gb == "week":
                    k = week_key(int(r[0]))
                elif gb == "project":
                    k = r[1]
                elif gb == "baseline":
                    k = bool(r[2])
                elif gb == "run_id":
                    k = r[3]
                else:
                    k = r[4] or None
                vals.setdefault(k, []).append(float(r[6]))
            groups = [{"key": k, **_stats(v)} for k, v in vals.items()]
            source = "raw"
    groups.sort(key=lambda g: (g["key"] is None, str(g["key"])))
    out = {"series": series, "group_by": gb, "source": source, "groups": groups}
    if truncated:
        out["truncated"] = True
    return out


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def is_binary(vals: list[float]) -> bool:
    return all(v in (0.0, 1.0) for v in vals)


def proportion_test(a: list[float], b: list[float]) -> float | None:
    na, nb = len(a), len(b)
    if not na or not nb:
        return None
    xa, xb = sum(a), sum(b)
    p = (xa + xb) / (na + nb)
    se = math.sqrt(p * (1 - p) * (1 / na + 1 / nb))
    if se == 0:
        return 0.0
    return (xb / nb - xa / na) / se


def mann_whitney(a: list[float], b: list[float]) -> float | None:
    """z of U for b (positive = b ranks higher); average ranks, tie-corrected variance, normal approximation."""
    na, nb = len(a), len(b)
    if not na or not nb:
        return None
    allv = sorted([(v, 0) for v in a] + [(v, 1) for v in b], key=lambda t: t[0])
    n = na + nb
    ranks = [0.0] * n
    ties = 0.0
    i = 0
    while i < n:
        j = i
        while j + 1 < n and allv[j + 1][0] == allv[i][0]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = avg
        t = j - i + 1
        ties += t ** 3 - t
        i = j + 1
    rb = sum(r for r, (_, g) in zip(ranks, allv) if g == 1)
    u = rb - nb * (nb + 1) / 2
    mu = na * nb / 2
    var = na * nb / 12 * ((n + 1) - ties / (n * (n - 1))) if n > 1 else 0.0
    if var <= 0:
        return 0.0
    return (u - mu) / math.sqrt(var)


def compare_values(a: list[float], b: list[float], test: str = "auto") -> dict:
    if test not in ("auto", "proportion", "mann_whitney"):
        raise CapError("invalid", "test must be auto, proportion or mann_whitney")
    if test == "auto":
        test = "proportion" if is_binary(a + b) else "mann_whitney"
    z = proportion_test(a, b) if test == "proportion" else mann_whitney(a, b)
    p = math.erfc(abs(z) / math.sqrt(2)) if z is not None else None
    sa, sb = _stats(a), _stats(b)
    ma, mb = sa["mean"], sb["mean"]
    rel = (mb - ma) / abs(ma) if ma not in (None, 0) and mb is not None else None
    return {"n_a": sa["n"], "n_b": sb["n"], "mean_a": ma, "mean_b": mb, "sd_a": sa["sd"], "sd_b": sb["sd"],
            "rel_delta": rel, "z": z, "p": p, "test": test}


# ---------------------------------------------------------------------------
# kh.served join
# ---------------------------------------------------------------------------


def served_units(c, ns: str, item_ids: set[str], since_ms: int | None = None) -> dict[str, set[str]]:
    """item_id -> units a `kh.served` event served it to (event unit_id or body unit_id)."""
    out: dict[str, set[str]] = {i: set() for i in item_ids}
    if not item_ids or not sqldb.table_exists(c, "kh_event"):
        return out
    where, params = ["ns=?", "kind=?"], [ns, "kh.served"]
    if since_ms is not None:
        where.append("ts_ms>=?")
        params.append(int(since_ms))
    rows = c.execute(f"SELECT unit_id, body_json FROM kh_event WHERE {' AND '.join(where)} ORDER BY seq DESC "
                     f"LIMIT {SERVED_SCAN_MAX}", params).fetchall()
    for r in rows:
        body = core.loads(r[1], {}) or {}
        unit = r[0] or body.get("unit_id")
        if not unit:
            continue
        for iid in body.get("item_ids") or []:
            if iid in out:
                out[iid].add(str(unit))
    return out


def _filter_rows(ctx: Ctx, c, series: str, f: dict) -> list:
    if not isinstance(f, dict):
        raise CapError("invalid", "a and b must be filter objects")
    unknown = set(f) - {"project", "run_id", "unit_ids", "baseline", "from", "to", "dims", "served_item",
                        "not_served_item"}
    if unknown:
        raise CapError("invalid", f"unknown filter keys: {', '.join(sorted(unknown))}")
    where, params = ["ns=?", "series=?"], [ctx.ns, series]
    _project_clause(ctx, f.get("project"), where, params)
    if f.get("run_id"):
        where.append("run_id=?")
        params.append(str(f["run_id"]))
    base = _bool_param(f.get("baseline"), "baseline")
    if base is not None:
        where.append("is_baseline=?")
        params.append(1 if base else 0)
    fr, to = core.parse_ms(f.get("from"), "from"), core.parse_ms(f.get("to"), "to")
    if fr is not None:
        where.append("ts_ms>=?")
        params.append(fr)
    if to is not None:
        where.append("ts_ms<?")
        params.append(to)
    rows = c.execute(f"SELECT ts_ms, unit_id, dims_json, val FROM kh_metric WHERE {' AND '.join(where)} "
                     f"ORDER BY ts_ms LIMIT {RAW_MAX}", params).fetchall()
    units = set(core.csv(f.get("unit_ids"))) if f.get("unit_ids") is not None else None
    dims = f.get("dims") or {}
    if not isinstance(dims, dict):
        raise CapError("invalid", "dims filter must be an object")
    served, not_served = f.get("served_item"), f.get("not_served_item")
    smap = served_units(c, ctx.ns, {x for x in (served, not_served) if x})
    out = []
    for r in rows:
        unit = r[1] or ""
        if units is not None and unit not in units:
            continue
        if dims:
            d = core.loads(r[2], {}) or {}
            if any(d.get(k) != v for k, v in dims.items()):
                continue
        if served and unit not in smap.get(served, set()):
            continue
        if not_served and (not unit or unit in smap.get(not_served, set())):
            continue
        out.append(r)
    return out


def compare(ctx: Ctx, body: dict) -> dict:
    series = body.get("series")
    _metric_type(ctx, series, "read")
    per_unit = bool(body.get("per_unit", False))
    with sqldb.connect() as c:
        ra = _filter_rows(ctx, c, series, body.get("a") or {})
        rb = _filter_rows(ctx, c, series, body.get("b") or {})

    def vals(rows):
        if not per_unit:
            return [float(r[3]) for r in rows]
        by: dict[str, list] = {}
        for r in rows:
            if r[1]:
                by.setdefault(r[1], []).append(float(r[3]))
        return [sum(v) / len(v) for v in by.values()]

    out = compare_values(vals(ra), vals(rb), body.get("test") or "auto")
    out["per_unit"] = per_unit
    return out


# ---------------------------------------------------------------------------
# Rollback (D12)
# ---------------------------------------------------------------------------


def _latest_per_unit(rows) -> dict[str, tuple[int, float]]:
    out: dict[str, tuple[int, float]] = {}
    for ts, unit, val in rows:
        if not unit:
            continue
        cur = out.get(unit)
        if cur is None or int(ts) > cur[0]:
            out[unit] = (int(ts), float(val))
    return out


def _withdraw(c, ns: str, item_id: str) -> bool:
    """active -> contested -> withdrawn (reason auto_rollback), conditional on the item still being active."""
    items = None
    try:
        from app.caps import module

        items = module("items")
    except Exception:  # noqa: BLE001
        items = None
    fn = getattr(items, "rollback_transition", None) if items is not None else None
    if callable(fn):
        return bool(fn(c, ns, item_id, "auto_rollback"))
    now = sqldb.now_ms(c)
    cur = c.execute("UPDATE kh_item SET status='contested', status_reason='auto_rollback', rev=rev+1, updated_ms=? "
                    "WHERE ns=? AND item_id=? AND status='active'", (now, ns, item_id))
    if not cur.rowcount or cur.rowcount <= 0:
        return False
    c.execute("UPDATE kh_item SET status='withdrawn', status_reason='auto_rollback', rev=rev+1, updated_ms=? "
              "WHERE ns=? AND item_id=? AND status='contested'", (now, ns, item_id))
    return True


def evaluate_item(c, st: core.NsState, item: dict, conf: dict, now: int) -> dict:
    series = item["guard_metric"]
    sc = series_conf(st, series)
    direction = item.get("metric_dir") or sc.get("dir") or "up"
    start = int(item.get("activated_ms") or now) - int(conf["window_days"]) * DAY_MS
    where, params = ["ns=?", "series=?", "is_baseline=0", "unit_id<>''", "ts_ms>=?"], [st.ns, series, start]
    if item.get("scope") != "cross" and item.get("project"):
        where.append("project=?")
        params.append(item["project"])
    rows = c.execute(f"SELECT ts_ms, unit_id, val FROM kh_metric WHERE {' AND '.join(where)} LIMIT {RAW_MAX}",
                     params).fetchall()
    latest = _latest_per_unit(rows)
    served = served_units(c, st.ns, {item["item_id"]}, start).get(item["item_id"], set())
    act = int(item.get("activated_ms") or 0)
    treat = [v for u, (_ts, v) in latest.items() if u in served]
    if conf["control"] == "pre_activation":
        ctrl = [v for u, (ts, v) in latest.items() if u not in served and ts < act]
    else:
        ctrl = [v for u, (_ts, v) in latest.items() if u not in served]
    res = compare_values(ctrl, treat, conf.get("test") or "auto")
    rel = res["rel_delta"]
    worse = rel is not None and ((direction == "down" and rel >= float(conf["min_rel"]))
                                 or (direction != "down" and rel <= -float(conf["min_rel"])))
    enough = res["n_a"] >= int(conf["min_n"]) and res["n_b"] >= int(conf["min_n"])
    sig = res["p"] is not None and res["p"] < float(conf["alpha"])
    reason = None if (enough and worse and sig) else ("too_few_units" if not enough else
                                                      ("not_worse" if not worse else "not_significant"))
    return {"item_id": item["item_id"], "series": series, "dir": direction, "n_treatment": res["n_b"],
            "n_control": res["n_a"], "mean_treatment": res["mean_b"], "mean_control": res["mean_a"],
            "rel_delta": rel, "z": res["z"], "p": res["p"], "test": res["test"], "fire": reason is None,
            "reason": reason}


def rollback(ns: str, dry_run: bool = False, item_ids: list[str] | None = None, ctx: Ctx | None = None) -> dict:
    st = core.ns_state(ns, fresh=True)
    if st is None or st.state != "active":
        raise CapError("ns_not_active", f"namespace '{ns}' is not active")
    conf = rollback_conf(st)
    holder = f"{sqldb.holder_id()}:{core.new_id()}"
    with sqldb.connect() as c:
        got = sqldb.lease_acquire(c, ns, ROLLBACK_LEASE, holder, 600_000)
    if got is None:
        raise CapError("lease_held", "a metrics rollback is already running for this namespace")
    results, fired = [], []
    try:
        with sqldb.connect() as c:
            if not sqldb.table_exists(c, "kh_item"):
                return {"ns": ns, "dry_run": dry_run, "enabled": bool(conf["enabled"]), "checked": 0,
                        "rolled_back": [], "results": []}
            where, params = ["ns=?", "status='active'", "guard_metric IS NOT NULL", "guard_metric<>''"], [ns]
            if item_ids:
                where.append(f"item_id IN ({', '.join('?' for _ in item_ids)})")
                params += list(item_ids)
            items = [{k: r[k] for k in r.keys()} for r in c.execute(
                f"SELECT item_id, project, scope, guard_metric, metric_dir, activated_ms FROM kh_item "
                f"WHERE {' AND '.join(where)}", params).fetchall()]
            now = sqldb.now_ms(c)
            for it in items:
                res = evaluate_item(c, st, it, conf, now)
                if res["fire"] and not dry_run:
                    res["rolled_back"] = _withdraw(c, ns, it["item_id"])
                    if res["rolled_back"]:
                        fired.append(res)
                results.append(res)
    finally:
        with sqldb.connect() as c:
            sqldb.lease_release(c, ns, ROLLBACK_LEASE, holder)
    for res in fired:
        core.emit(ns, "kh.item_rollback", {k: res[k] for k in ("item_id", "series", "n_treatment", "n_control",
                                                                "mean_treatment", "mean_control", "rel_delta", "p",
                                                                "test")}, ctx)
    return {"ns": ns, "dry_run": dry_run, "enabled": bool(conf["enabled"]), "checked": len(results),
            "rolled_back": [r["item_id"] for r in fired], "results": results}


def cron_rollback() -> int:
    n = 0
    with sqldb.connect() as c:
        if not sqldb.table_exists(c, "kh_ns"):
            return 0
        nss = [r[0] for r in c.execute("SELECT ns FROM kh_ns WHERE state='active'").fetchall()]
    for ns in nss:
        st = core.ns_state(ns, fresh=True)
        if st is None or not rollback_conf(st)["enabled"]:
            continue
        try:
            n += len(rollback(ns)["rolled_back"])
        except CapError:
            continue
    return n


def cron_retention() -> int:
    """Prune raw rows of series with retention_days (rollup kept)."""
    deleted = 0
    with sqldb.connect() as c:
        if not sqldb.table_exists(c, "kh_type"):
            return 0
        rows = c.execute("SELECT t.ns, t.type, t.retention_days FROM kh_type t JOIN kh_ns n ON n.ns=t.ns "
                         "WHERE t.kind_of='metric' AND t.retention_days IS NOT NULL AND n.state IN "
                         "('active','suspended')").fetchall()
        now = sqldb.now_ms(c)
        for ns, series, days in rows:
            cur = c.execute("DELETE FROM kh_metric WHERE ns=? AND series=? AND ts_ms<?",
                            (ns, series, now - int(days) * DAY_MS))
            deleted += int(cur.rowcount or 0)
    return deleted


CRONS = [("metrics_rollback", rollback_interval_s(), cron_rollback), ("metrics_retention", 86400, cron_retention)]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def attach(app: FastAPI) -> None:
    tag = ["v1 metrics"]

    @app.post("/v1/{ns}/metrics:compare", tags=tag, summary="A/B compare two filtered sets of a series")
    async def post_compare(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require("metrics", "read")
        body = await core.read_object(request)
        return await core.run(compare, ctx, body)

    @app.post("/v1/{ns}/metrics:rollback", tags=tag, summary="Run the automatic item rollback now")
    @core.idempotent
    async def post_rollback(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        if not (ctx.is_admin or ctx.role == "orchestrator"):
            raise CapError("forbidden", "namespace admin or orchestrator only")
        body = await core.read_object(request)
        ids = body.get("item_ids")
        if ids is not None and (not isinstance(ids, list) or not all(isinstance(x, str) for x in ids)):
            raise CapError("invalid", "item_ids must be a list of item ids")
        out = await core.run(rollback, ns, bool(body.get("dry_run", False)), ids, ctx)
        return core.reply(out)

    @app.get("/v1/{ns}/metrics:aggregate", tags=tag, summary="Aggregate a series")
    async def get_aggregate(ns: str, series: str = Query(...), group_by: str = Query("day"),
                            window: str | None = Query(None), from_: str | None = Query(None, alias="from"),
                            to: str | None = Query(None), project: str | None = Query(None),
                            baseline: str | None = Query(None), ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require("metrics", "read")
        fr, t = core.parse_ms(from_, "from"), core.parse_ms(to, "to")
        w = parse_window(window)
        if w is not None and fr is None:
            fr = (t or core.now_ms()) - w
        return await core.run(aggregate, ctx, series, group_by, fr, t, project, _bool_param(baseline, "baseline"))

    @app.post("/v1/{ns}/metrics", tags=tag, summary="Write metric observations")
    @core.idempotent
    async def post_metrics(request: Request, ns: str, ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require("metrics", "write")
        body = await core.read_json(request)
        if isinstance(body, list):
            obs = body
        elif isinstance(body, dict) and isinstance(body.get("observations"), list):
            obs = body["observations"]
        elif isinstance(body, dict):
            obs = [body]
        else:
            raise CapError("invalid", "send {observations:[...]}, a list or one observation")
        return core.reply(await core.run(write, ctx, obs))

    @app.get("/v1/{ns}/metrics", tags=tag, summary="List metric observations")
    async def get_metrics(ns: str, series: str | None = Query(None), project: str | None = Query(None),
                          run_id: str | None = Query(None), unit_id: str | None = Query(None),
                          baseline: str | None = Query(None), from_: str | None = Query(None, alias="from"),
                          to: str | None = Query(None), limit: str | None = Query(None),
                          offset: str | None = Query(None), ctx: Ctx = Depends(core.ns_ctx)):
        ctx.require("metrics", "read")
        lim = core.clamp_limit(limit, 100, LIST_MAX)
        off = core.parse_int(offset, "offset", 0, 0)
        obs = await core.run(list_obs, ctx, series, project, run_id, unit_id, _bool_param(baseline, "baseline"),
                             core.parse_ms(from_, "from"), core.parse_ms(to, "to"), lim, off)
        return {"observations": obs, "limit": lim, "offset": off}
