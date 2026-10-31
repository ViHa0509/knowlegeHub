"""Chroma `where` / `where_document` semantics in Python, plus the SQL prefilter translation shared by
the HANA and SQLite vector adapters (spec §1.7.1).
"""
from __future__ import annotations

import re
from typing import Any, Callable

_KEY_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_IN_MAX = 500
_MISSING = object()


def _cmp_ok(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return False
    return isinstance(a, (int, float)) and isinstance(b, (int, float)) or (isinstance(a, str) and isinstance(b, str))


def _eq(a, b) -> bool:
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def _match_op(val, op: str, arg) -> bool:
    if val is _MISSING:
        return False  # a missing field never matches (also for $ne / $nin)
    if op == "$eq":
        return _eq(val, arg)
    if op == "$ne":
        return not _eq(val, arg)
    if op == "$in":
        return any(_eq(val, x) for x in (arg or []))
    if op == "$nin":
        return not any(_eq(val, x) for x in (arg or []))
    if op in ("$gt", "$gte", "$lt", "$lte"):
        if not _cmp_ok(val, arg):
            return False
        if op == "$gt":
            return val > arg
        if op == "$gte":
            return val >= arg
        if op == "$lt":
            return val < arg
        return val <= arg
    raise ValueError(f"unsupported where operator {op}")


def matches_where(meta: dict | None, where: dict | None) -> bool:
    """Full Chroma semantics: $and, $or, $eq, $ne, $in, $nin, $gt, $gte, $lt, $lte."""
    if not where:
        return True
    meta = meta or {}
    for key, cond in where.items():
        if key == "$and":
            if not all(matches_where(meta, w) for w in (cond or [])):
                return False
        elif key == "$or":
            if not any(matches_where(meta, w) for w in (cond or [])):
                return False
        else:
            val = meta.get(key, _MISSING)
            if val is None:
                val = _MISSING
            if isinstance(cond, dict):
                for op, arg in cond.items():
                    if not _match_op(val, op, arg):
                        return False
            else:
                if not _match_op(val, "$eq", cond):
                    return False
    return True


def matches_document(doc: str | None, where_document: dict | None) -> bool:
    """$contains, $not_contains, $and, $or (case-sensitive substring)."""
    if not where_document:
        return True
    doc = doc or ""
    for key, cond in where_document.items():
        if key == "$contains":
            if str(cond) not in doc:
                return False
        elif key == "$not_contains":
            if str(cond) in doc:
                return False
        elif key == "$and":
            if not all(matches_document(doc, w) for w in (cond or [])):
                return False
        elif key == "$or":
            if not any(matches_document(doc, w) for w in (cond or [])):
                return False
        else:
            raise ValueError(f"unsupported where_document operator {key}")
    return True


def sql_filter(where: dict | None, json_value: Callable[[str], str]) -> tuple[str, list[Any], bool]:
    """Translate the SQL-safe part of a where filter.

    Only top-level (or top-level `$and`) terms with string $eq / string $in (<=500 values) on keys matching
    ^[A-Za-z0-9_]{1,64}$ are translated. Returns (sql, params, exact) where `sql` is "" or starts with " AND ",
    and exact=True when nothing was left out (SQL alone decides).
    """
    if not where:
        return "", [], True
    terms: list[tuple[str, Any]] = []
    exact = True

    def walk(w: dict):
        nonlocal exact
        for k, v in w.items():
            if k == "$and" and isinstance(v, list):
                for sub in v:
                    if isinstance(sub, dict):
                        walk(sub)
                    else:
                        exact = False
            elif k.startswith("$"):
                exact = False
            else:
                terms.append((k, v))

    walk(where)
    sql: list[str] = []
    params: list[Any] = []
    for k, cond in terms:
        if not _KEY_RE.match(k):
            exact = False
            continue
        if isinstance(cond, str):
            cond = {"$eq": cond}
        if not isinstance(cond, dict):
            exact = False
            continue
        for op, arg in cond.items():
            if op == "$eq" and isinstance(arg, str):
                sql.append(f"{json_value(k)} = ?")
                params.append(arg)
            elif op == "$in" and isinstance(arg, list) and arg and len(arg) <= _IN_MAX \
                    and all(isinstance(x, str) for x in arg):
                sql.append(f"{json_value(k)} IN ({', '.join('?' for _ in arg)})")
                params.extend(arg)
            else:
                exact = False
    if not sql:
        return "", [], exact and not terms
    return " AND " + " AND ".join(sql), params, exact
