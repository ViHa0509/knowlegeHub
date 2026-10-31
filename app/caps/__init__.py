"""Stage F capability modules (spec §6.0): registration, switches, discovery, crons.

`MODULES` is in dependency order (registry first). A module file that is absent is skipped, so modules can land
independently. Switch: `KH_CAP_<NAME>` (DEFAULT_ON modules are on unless off/false/0/no; DEFAULT_ON=False modules
are on only for on/true/1/yes). A switched-off module is not init'ed, has no routes and is not listed.

Each module exposes NAME, VERSION, STATUS, LIMITS, init(c), attach(app), optional DEFAULT_ON, CRONS
[(lease_name, interval_s, fn)], TABLES, LOCAL_TABLES, SUNSET.
"""
from __future__ import annotations

import importlib
import logging
import os
import threading

from fastapi import FastAPI

from app import config, sqldb
from app.caps import core

log = logging.getLogger(__name__)

MODULES = [
    "registry", "coord", "events", "principals", "records", "rules", "redact", "items", "retrieval", "vault",
    "packs", "metrics", "outbox", "lifecycle",
    # roadmap E-5 (DEFAULT_ON = False)
    "people", "daily", "outcomes", "learn", "improve",
]
CAPABILITIES: list[dict] = []
LOADED: list = []
_OFF = {"off", "false", "0", "no"}
_ON = {"on", "true", "1", "yes"}


def enabled(name: str, default_on: bool) -> bool:
    v = (os.environ.get(f"KH_CAP_{name.upper()}") or "").strip().lower()
    if default_on:
        return v not in _OFF
    return v in _ON


def _import(modname: str):
    full = f"app.caps.{modname}"
    try:
        return importlib.import_module(full)
    except ModuleNotFoundError as e:
        if e.name == full:
            return None
        raise


def active_modules() -> list:
    out = []
    for m in MODULES:
        mod = _import(m)
        if mod is None:
            continue
        if not enabled(getattr(mod, "NAME", m), bool(getattr(mod, "DEFAULT_ON", True))):
            log.info("capability %s switched off", getattr(mod, "NAME", m))
            continue
        out.append(mod)
    return out


def module(name: str):
    """A loaded (enabled) module by NAME, else None."""
    for mod in LOADED:
        if getattr(mod, "NAME", None) == name:
            return mod
    return None


def init_all(mods: list | None = None) -> None:
    with sqldb.connect() as c:
        for mod in (mods if mods is not None else LOADED):
            mod.init(c)


def attach_all(app: FastAPI) -> None:
    core.install(app)
    mods = active_modules()
    LOADED[:] = mods
    init_all(mods)
    for mod in mods:
        mod.attach(app)
    CAPABILITIES[:] = []
    for mod in mods:
        cap = {"name": mod.NAME, "version": mod.VERSION, "status": getattr(mod, "STATUS", "beta"),
               "limits": dict(getattr(mod, "LIMITS", {}) or {})}
        if getattr(mod, "SUNSET", None):
            cap["sunset"] = mod.SUNSET
        CAPABILITIES.append(cap)
    CAPABILITIES.sort(key=lambda c: c["name"])

    @app.get("/v1/capabilities", tags=["v1"], summary="Capability modules on this KH")
    async def capabilities():
        return {"kh_version": config.KH_VERSION, "api_versions": ["v1"], "capabilities": CAPABILITIES}

    @app.get("/v1/openapi.json", tags=["v1"], summary="OpenAPI of the /v1 routes")
    async def openapi_v1():
        spec = dict(app.openapi())
        spec["paths"] = {p: v for p, v in (spec.get("paths") or {}).items() if p.startswith("/v1/")}
        return spec


def attach(app: FastAPI) -> None:
    attach_all(app)


# ---------------------------------------------------------------------------
# Crons
# ---------------------------------------------------------------------------

_stop = threading.Event()
_threads: list[threading.Thread] = []


def _cron_loop(name: str, interval: float, fn) -> None:
    wait = min(float(interval), 30.0)
    while not _stop.wait(wait):
        try:
            sqldb.run_under_lease(name, float(interval) * 1.5, fn)
        except Exception as e:  # noqa: BLE001
            log.warning("cron %s failed: %s", name, e)
        wait = float(interval)


def start_crons() -> int:
    if not config._bool("KH_CAPS_CRONS", True):
        return 0
    _stop.clear()
    n = 0
    for mod in LOADED:
        for name, interval, fn in getattr(mod, "CRONS", []) or []:
            t = threading.Thread(target=_cron_loop, args=(name, interval, fn), daemon=True, name=f"kh-{name}")
            t.start()
            _threads.append(t)
            n += 1
    return n


def start(app: FastAPI) -> None:  # noqa: ARG001
    n = start_crons()
    log.info("caps crons started: %d", n)


def stop() -> None:
    _stop.set()
    _threads.clear()
