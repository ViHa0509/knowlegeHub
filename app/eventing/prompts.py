"""Prompts for learn-back (spec §4.9, `app/eventing/prompts.py`). Never raises.

For each served log with an `actor`, read that session's `kh.session.prompt` events (<= 5000) and attach those
typed after the session's previous log in the batch and at most `KH_DISTILL_PROMPT_WINDOW_S` (4 h) before the
log, newest kept within `KH_DISTILL_PROMPT_CHARS` (1500; a single long prompt is truncated).
"""
from __future__ import annotations

import logging

from app import config

log = logging.getLogger(__name__)

MAX_EVENTS = 5000
PROMPT_KIND = "kh.session.prompt"


def window_s() -> float:
    return config._float("KH_DISTILL_PROMPT_WINDOW_S", 4 * 3600)


def max_chars() -> int:
    return config._int("KH_DISTILL_PROMPT_CHARS", 1500)


def session_prompts(session: str, ns: str | None = None, email: str | None = None) -> list[dict]:
    from app.eventing import emit
    from app.eventing.store import get_store

    filters = {"session_id": session}
    if email:
        filters["ix_s1"] = email
    return get_store().read(ns or emit.namespace(), 0, kinds=[PROMPT_KIND], filters=filters, limit=MAX_EVENTS)


def attach_prompts(logs: list[dict]) -> list[dict]:
    """Adds `prompts: [str]` to every log dict (needs `ts`, optional `actor`) and removes `actor`."""
    from app import hotlog

    try:
        by_session: dict[str, list[dict]] = {}
        order = sorted(range(len(logs)), key=lambda i: hotlog.parse_ts(logs[i].get("ts")))
        prev_ts: dict[str, float] = {}
        cap = max_chars()
        win = window_s()
        for i in order:
            lg = logs[i]
            actor = lg.get("actor")
            lg["prompts"] = []
            if not actor:
                continue
            if actor not in by_session:
                try:
                    by_session[actor] = session_prompts(actor)
                except Exception as e:  # noqa: BLE001
                    log.debug("prompt read failed: %s", e)
                    by_session[actor] = []
            ts = hotlog.parse_ts(lg.get("ts"))
            lo = max(prev_ts.get(actor, float("-inf")), ts - win)
            prev_ts[actor] = ts
            cand = []
            for ev in by_session[actor]:
                t = (ev.get("ts_ms") or 0) / 1000.0
                if lo < t <= ts:
                    cand.append((t, str((ev.get("body") or {}).get("text") or "")))
            cand.sort(key=lambda x: x[0], reverse=True)
            kept, used = [], 0
            for _t, text in cand:
                if not text:
                    continue
                room = cap - used
                if room <= 0:
                    break
                if len(text) > room:
                    if kept:
                        break
                    text = text[:room]
                kept.append(text)
                used += len(text)
            lg["prompts"] = list(reversed(kept))
    except Exception as e:  # noqa: BLE001
        log.debug("attach_prompts failed: %s", e)
        for lg in logs:
            lg.setdefault("prompts", [])
    for lg in logs:
        lg.pop("actor", None)
    return logs
