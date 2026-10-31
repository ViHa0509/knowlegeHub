"""Fixtures for the kh_worker tests (spec §4.6). No network, no real LLM: fakes for the KH client and the model."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

WORKER = Path(__file__).resolve().parents[2] / "worker"
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))

from kh_worker.budget import Budget  # noqa: E402
from kh_worker.jobs import Ctx  # noqa: E402
from kh_worker.llm import LLMResult, Metered  # noqa: E402


class FakeLLM:
    """Scripted model. `answers` is a list consumed in order; each entry is a dict (the JSON answer), an
    Exception (raised), or a callable(system, user, schema) -> dict."""

    name = "fake"

    def __init__(self, answers=None, model="claude-opus-5-5", billed=True, cost=0.01):
        self.answers = list(answers or [])
        self.model = model
        self.billed = billed
        self.cost = cost
        self.calls: list[dict] = []
        self.batches: list[list[dict]] = []

    def _next(self, system, user, schema):
        a = self.answers.pop(0) if self.answers else {"items": [], "contexts": [], "signal_words": []}
        if callable(a) and not isinstance(a, type):
            a = a(system, user, schema)
        if isinstance(a, Exception):
            raise a
        return LLMResult(json=a, usage={"input_tokens": 10, "output_tokens": 10}, model=self.model,
                         cost_usd=self.cost, billed=self.billed)

    def complete(self, system, user, schema, max_tokens=16000):
        self.calls.append({"system": system, "user": user, "schema": schema, "max_tokens": max_tokens})
        return self._next(system, user, schema)

    def complete_many(self, requests):
        self.batches.append(requests)
        out = []
        for r in requests:
            try:
                out.append(self._next(r["system"], r["user"], r["schema"]))
            except Exception as e:  # noqa: BLE001
                out.append(e)
        return out


class FakeKH:
    """KH routes the jobs use. Queues are consumed in order; responses can be callables(body) or Exceptions."""

    def __init__(self):
        self.next_queue: dict[str, list] = {}
        self.submit_queue: list = []
        self.submits: list[dict] = []
        self.approves: list = []
        self.next_calls: list[str] = []
        self.daily_days: dict[str, list] = {}
        self.daily_submits: list[dict] = []
        self.daily_answers: list = []
        self.promote_data: dict = {}
        self.promote_submits: list[dict] = []
        self.promote_answers: list = []
        self.events: list = []

    @staticmethod
    def _answer(queue, body, default):
        a = queue.pop(0) if queue else default
        if callable(a):
            a = a(body)
        if isinstance(a, Exception):
            raise a
        return a

    def distill_next(self, project, limit=None):
        self.next_calls.append(project)
        q = self.next_queue.get(project) or []
        a = q.pop(0) if q else {"project": project, "batch_id": None, "logs": []}
        if isinstance(a, Exception):
            raise a
        return a

    def distill_submit(self, body):
        self.submits.append(body)
        n = len(body.get("items") or [])
        default = {"dry_run": bool(body.get("dry_run")), "batch_open": False,
                   "results": [{"n": i, "result": "accepted", "id": f"id{len(self.submits)}_{i}"} for i in range(n)]}
        return self._answer(self.submit_queue, body, default)

    def distill_approve(self, project, ids, share=None):
        self.approves.append((project, ids, share))
        return {"ok": True}

    def daily_next(self, project):
        a = self.daily_days.get(project, [])
        if isinstance(a, Exception):
            raise a
        return {"days": a}

    def daily_submit(self, body):
        self.daily_submits.append(body)
        return self._answer(self.daily_answers, body, {"ok": True})

    def promote_next(self):
        return self.promote_data

    def promote_submit(self, body):
        self.promote_submits.append(body)
        n = len(body.get("items") or [])
        default = {"dry_run": bool(body.get("dry_run")),
                   "results": [{"n": i, "result": "accepted", "id": f"p{i}"} for i in range(n)]}
        return self._answer(self.promote_answers, body, default)

    def event(self, kind, body):
        self.events.append((kind, body))


@pytest.fixture
def kh():
    return FakeKH()


@pytest.fixture
def make_ctx(kh):
    def make(llm, mode="on", projects=("alpha",), cap=3.0, **kw):
        budget = Budget(cap, 60.0, 0.0)
        return Ctx(kh=kh, llm=Metered(llm, budget), mode=mode, projects=list(projects),
                   log=logging.getLogger("kh_worker.test"), **kw)
    return make
