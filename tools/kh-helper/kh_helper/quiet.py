"""Quiet mode: a hook or tool never surfaces a red error.

Every failure is written to HOME/kh-helper.log; the person sees at most one polite line, at most once per
NOTICE_SECONDS.
"""
from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timezone

LOG_NAME = "kh-helper.log"
NOTICE_NAME = "slow-notice"
MAX_LOG_BYTES = 1_000_000
NOTICE_SECONDS = 600

SLOW = ("Knowledge Hub is responding slowly right now. Your work continues; KH will be used again on "
        "your next message.")
SLOW_STOP = ("Knowledge Hub is responding slowly right now, so the work log check was skipped for this reply.")
SLOW_TOOL = ("Knowledge Hub did not answer in time (slow connection). Carry on without it for this step and "
             "try again later; do not tell the person it is an error.")


def _home() -> str:
    from . import session
    return session.home()


def log(where: str, exc: BaseException | None = None, note: str | None = None) -> None:
    """Append '<ISO UTC> <where>: <note or exception type>' plus the traceback. Never raises."""
    try:
        home = _home()
        os.makedirs(home, exist_ok=True)
        path = os.path.join(home, LOG_NAME)
        try:
            if os.path.getsize(path) > MAX_LOG_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        what = note or (type(exc).__name__ if exc is not None else "")
        lines = [f"{stamp} {where}: {what}\n"]
        if exc is not None:
            lines.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        with open(path, "a", encoding="utf-8") as fh:
            fh.writelines(lines)
    except Exception:  # noqa: BLE001 - logging must never raise
        pass


def notice_due() -> bool:
    """True at most once per NOTICE_SECONDS (tracked by the mtime of HOME/slow-notice)."""
    try:
        home = _home()
        os.makedirs(home, exist_ok=True)
        path = os.path.join(home, NOTICE_NAME)
        now = time.time()
        try:
            if now - os.path.getmtime(path) < NOTICE_SECONDS:
                return False
        except OSError:
            pass
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("")
        os.utime(path, (now, now))
        return True
    except Exception:  # noqa: BLE001
        return False
