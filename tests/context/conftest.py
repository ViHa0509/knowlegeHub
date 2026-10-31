"""Helpers for the §3 context-engine tests (open mode unless a test enables sign-in)."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import pytest

from app import kernel


def iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def uid():
    return uuid.uuid4().hex[:8]


@pytest.fixture
def add_row(client):
    def _add(text, scope="knowledge", project=None, **meta):
        body = {"text": text, "scope": scope, "metadata": {"scope": scope, **meta}}
        if project:
            body["metadata"]["project"] = project
        if scope == "log":
            body["metadata"].setdefault("ts", iso())
            body["metadata"].setdefault("topic", "t")
        r = client.post("/vector/add", json=body)
        assert r.status_code == 200, r.text
        return r.json()["vector_id"]
    return _add


@pytest.fixture
def common_slots():
    return [r["slot"] for r in kernel.R.list_rules("common")]
