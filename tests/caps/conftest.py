"""Helpers for /v1 capability tests (§6). Open mode unless a test enables sign-in."""
from __future__ import annotations

import uuid

import pytest

from tests.auth_helpers import bearer, enable_signin, make_token  # noqa: F401


def new_ns() -> str:
    return "t" + uuid.uuid4().hex[:12]


def new_project() -> str:
    from app import projects

    pid = "cp-" + uuid.uuid4().hex[:8]
    projects.get_or_create_project(pid)
    return pid


EVENT_MANIFEST = {
    "schemas": [{"name": "step", "version": 1,
                 "schema": {"type": "object", "properties": {"n": {"type": "integer"}, "who": {"type": "string"}},
                            "required": ["n"]}}],
    "types": [
        {"type": "step.done", "kind_of": "event", "schema": "step", "index_fields": {"who": "s", "n": "n"}},
        {"type": "unit.write", "kind_of": "event", "fenced": True},
        {"type": "secret.ev", "kind_of": "event", "read_roles": ["judge"]},
        {"type": "run.start", "kind_of": "event", "required_envelope": ["run_id"]},
    ],
}


def attach(client, ns=None, projects=None, manifest=None, headers=None):
    ns = ns or new_ns()
    projects = projects if projects is not None else [new_project()]
    r = client.put(f"/v1/admin/ns/{ns}", json={"projects": projects}, headers=headers or {})
    assert r.status_code == 201, r.text
    if manifest is not None:
        m = client.put(f"/v1/{ns}/manifest", json=manifest, headers=headers or {})
        assert m.status_code == 200, m.text
    return ns, projects, r.json()


@pytest.fixture
def ns_env(client):
    """(client, ns, projects) with EVENT_MANIFEST applied, open mode."""
    ns, projects, _ = attach(client, manifest=EVENT_MANIFEST)
    return client, ns, projects
