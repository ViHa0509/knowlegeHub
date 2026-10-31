"""Helpers for the §4 learning-loop tests (open mode unless a test enables sign-in)."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

import pytest

ACTION = "troubleshoot problem"   # an every-project action of the seeded policy table


def iso(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def uid():
    return uuid.uuid4().hex[:8]


@pytest.fixture
def proj(uid, monkeypatch):
    """A fresh project id that has consented to distillation; sampling off so picks are deterministic."""
    pid = f"dl-{uid}"
    monkeypatch.setenv("KH_DISTILL_PROJECTS", pid)
    monkeypatch.setenv("KH_DISTILL_SAMPLE_PCT", "0")
    return pid


@pytest.fixture
def add_log(client):
    """add_log(project, text, ts=None (epoch or iso), topic="t", actor=None) -> vector id."""
    base = [time.time() - 3600]

    def _add(project, text, ts=None, topic="t", actor=None, scope="log", **meta):
        if ts is None:
            base[0] += 1
            ts = base[0]
        body = {"text": text, "scope": scope,
                "metadata": {"scope": scope, "project": project, "topic": topic,
                             "ts": iso(ts) if isinstance(ts, (int, float)) else ts, **meta}}
        headers = {"X-KH-Actor": actor} if actor else {}
        r = client.post("/vector/add", json=body, headers=headers)
        assert r.status_code == 200, r.text
        return r.json()["vector_id"]
    return _add


@pytest.fixture
def add_know(client):
    def _add(project, text, **meta):
        r = client.post("/vector/add", json={"text": text, "scope": "knowledge",
                                             "metadata": {"scope": "knowledge", "project": project, **meta}})
        assert r.status_code == 200, r.text
        return r.json()["vector_id"]
    return _add


def item(sources, title, text, kind="lesson", action=ACTION, context="-", **kw):
    return {"kind": kind, "action": action, "context": context, "title": title, "text": text,
            "entities": kw.pop("entities", []), "sources": list(sources), **kw}


@pytest.fixture
def distil(client):
    """distil(project, items, batch=None, **body) -> (next json, submit response). Pulls a batch first."""
    def _run(project, items_fn=None, **body):
        nx = client.get("/distill/next", params={"project": project})
        assert nx.status_code == 200, nx.text
        nj = nx.json()
        items = items_fn(nj) if callable(items_fn) else (items_fn or [])
        sb = {"project": project, "batch_id": nj["batch_id"], "items": items, "distilled_by": "test", **body}
        return nj, client.post("/distill/submit", json=sb)
    return _run
