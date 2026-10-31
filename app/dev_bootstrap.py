"""DEV ONLY bootstrap maintainer (spec §2.2).

With `KH_DEV_BOOTSTRAP=1`, `KH_DEV_BOOTSTRAP_TOKEN` of at least 32 characters, not running on Cloud Foundry and
not on the production store (HANA), the extra bearer `khdev.<token>` signs in as the maintainer
`KH_DEV_BOOTSTRAP_EMAIL` (default `dev-admin@kh.local`). Anything else: the bearer is not special.
"""
from __future__ import annotations

import hmac
import logging
import os

from app import config

log = logging.getLogger(__name__)

MIN_TOKEN = 32
PREFIX = "khdev."


def enabled() -> bool:
    if (config._env("KH_DEV_BOOTSTRAP") or "").lower() not in ("1", "true", "on", "yes"):
        return False
    if len(config._env("KH_DEV_BOOTSTRAP_TOKEN") or "") < MIN_TOKEN:
        return False
    if os.environ.get("CF_INSTANCE_GUID") or os.environ.get("VCAP_APPLICATION"):
        return False
    if config.STORE_BACKEND == "hana":
        return False
    return True


def email() -> str:
    return (config._env("KH_DEV_BOOTSTRAP_EMAIL", "dev-admin@kh.local") or "dev-admin@kh.local").strip().lower()


def principal_for(token: str):
    """Principal for `khdev.<token>` when the bootstrap is on and the token matches, else None."""
    if not enabled() or not token.startswith(PREFIX):
        return None
    want = (config._env("KH_DEV_BOOTSTRAP_TOKEN") or "").encode()
    if not hmac.compare_digest(token[len(PREFIX):].encode(), want):
        return None
    from app.auth import KH_PERMISSIONS, PROJECT_PERMISSIONS, Principal

    return Principal(email=email(), role="maintainer",
                     kh_permissions=set(KH_PERMISSIONS) | set(PROJECT_PERMISSIONS),
                     every_project=set(PROJECT_PERMISSIONS))
