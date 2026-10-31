"""Personal projects (spec §1.14, §2.8-2.9), minimal data layer. The §2 builder may extend it.

    personal_id(email)                  "personal-" + slug(email)   (slug: lower, non [a-z0-9] runs -> "-")
    is_personal(pid)
    OWN_SCOPES                          scopes that land in the caller's personal project when no project is named
    write_target(scope, project, principal=None)  -> project id or None
    target(scope, project)              alias of write_target with the current principal
    ensure(pid, owner_email=None)       get-or-create the personal project (role "project")
    owns(email, pid)                    pid is the personal project of email
    touch(email, encode=None)           ensure + register the C4 registry row (GET /auth/me)
"""
from __future__ import annotations

import re

from app import config

PREFIX = config.PERSONAL_PREFIX
SCOPE = config.PERSONAL_SCOPE
LESSON_SCOPE = config.PERSONAL_LESSON_SCOPE
OWN_SCOPES = (SCOPE, config.HOTLOG_SCOPE, "findings", LESSON_SCOPE)

_SLUG = re.compile(r"[^a-z0-9]+")


def slug(email: str) -> str:
    return _SLUG.sub("-", (email or "").strip().lower()).strip("-")


def personal_id(email: str) -> str:
    return PREFIX + slug(email)


def is_personal(pid: str | None) -> bool:
    return bool(pid) and str(pid).startswith(PREFIX)


def write_target(scope: str | None, project: str | None, principal=None) -> str | None:
    """A named project is kept; with sign-in on, an OWN_SCOPES write naming none goes to the personal project."""
    if project and str(project).strip():
        return str(project).strip()
    if principal is None:
        from app import auth

        principal = auth.current()
    if principal is not None and scope in OWN_SCOPES and getattr(principal, "email", None) \
            and not getattr(principal, "ns", None):
        return personal_id(principal.email)
    return project or None


def target(scope: str | None, project: str | None) -> str | None:
    return write_target(scope, project)


def ensure(pid: str, owner_email: str | None = None) -> dict:
    from app import projects

    return projects.get_or_create_project(pid, role="project",
                                          descriptor=f"Personal workspace of {owner_email}" if owner_email else "")


def owns(email: str | None, pid: str | None) -> bool:
    """True when `pid` is the personal project of `email` (access to a personal project is by e-mail)."""
    return bool(email) and bool(pid) and personal_id(email) == pid


def register(pid: str, encode=None) -> bool:
    """Write the C4 registry row of `pid` (only rows that changed are re-embedded). Never raises."""
    from app import projects

    try:
        projects.sync_orchestrator_registry(encode)
        return True
    except Exception as e:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning("C4 registry row for %s failed: %s", pid, e)
        return False


def touch(email: str, encode=None) -> dict:
    """Ensure the personal project of `email` exists and is registered in C4 (§2.8, `GET /auth/me`)."""
    pid = personal_id(email)
    from app import projects

    existed = projects.exists(pid)
    entry = ensure(pid, email)
    if not existed:
        register(pid, encode)
    else:
        from app import vector_store as vs

        h = vs.get_existing(vs.part_collection_name("knowledge", projects.ORCHESTRATOR))
        try:
            have = h.get(ids=[projects.REGISTRY_PREFIX + pid]) if h is not None else {"ids": []}
        except Exception:  # noqa: BLE001
            have = {"ids": []}
        if not (have.get("ids") or []):
            register(pid, encode)
    return entry
