"""KH 7.x spaces (v2 project frame), served over the rebuild's legacy stores.

KH_SPACES (default on) attaches the routes; KH 7.1 ships the same switch (off by default on a desktop
install, on in Prod). Set KH_SPACES=off to serve the v6.5 surface only.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from app import config

log = logging.getLogger(__name__)


def switch(name: str, default: bool = True) -> bool:
    """KH 7.1 switches take on/off (also true/false, 1/0, yes/no)."""
    v = (config._env(name) or "").strip().strip("'\"").lower()
    if not v:
        return default
    return v in ("on", "true", "1", "yes")


def enabled() -> bool:
    return switch("KH_SPACES", True)


def attach(app: FastAPI) -> None:
    if not enabled():
        log.info("Spaces off (KH_SPACES): no /spaces routes")
        return
    from app.spaces import api

    api.attach(app)
    log.info("Spaces routes attached (/spaces, /spaces/match, /spaces/{space}/...)")
