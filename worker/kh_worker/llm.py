"""LLM door (spec §4.6): `complete(system, user, schema, max_tokens) -> LLMResult` and `complete_many`.

Adapters: `anthropic` (Messages API, adaptive thinking, structured JSON output, server-side fallbacks; batches for
complete_many; `anthropic` is imported lazily), `claude_cli` (`claude -p ... --json-schema`), `dry` (canned output
from KH_WORKER_DRY_FIXTURE or a minimal instance of the schema; never billed). `Metered(llm, budget)` wraps an
adapter with the spend caps.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from kh_worker.budget import Budget, cost_of, estimate, tokens_of

CLAUDE_CODE_SYSTEM = "You are Claude Code, Anthropic's official CLI for Claude."


class LLMError(Exception):
    pass


@dataclass
class LLMResult:
    json: Any
    usage: dict = field(default_factory=dict)
    model: str = ""
    cost_usd: float = 0.0
    billed: bool = False
    text: str = ""
    stop_reason: str | None = None


def minimal_instance(schema: dict | None) -> Any:
    s = schema or {}
    if "enum" in s and s["enum"]:
        return s["enum"][0]
    t = s.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), t[0] if t else "object")
    if t == "object" or (t is None and "properties" in s):
        props = s.get("properties") or {}
        return {k: minimal_instance(props.get(k) or {}) for k in (s.get("required") or [])}
    if t == "array":
        return []
    if t == "string":
        return ""
    if t in ("number", "integer"):
        return 0
    if t == "boolean":
        return False
    return None


class DryLLM:
    name = "dry"
    billed = False

    def __init__(self, model: str = "dry", fixture: str | None = None,
                 responder: Optional[Callable[[str, str, dict], Any]] = None):
        self.model = model
        self.fixture_path = fixture if fixture is not None else os.environ.get("KH_WORKER_DRY_FIXTURE")
        self.responder = responder
        self.calls: list[dict] = []

    def _fixture(self, schema: dict) -> Any:
        if not self.fixture_path:
            return None
        try:
            with open(self.fixture_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        title = (schema or {}).get("title")
        if isinstance(data, dict) and title and title in data:
            return data[title]
        return data

    def complete(self, system: str, user: str, schema: dict, max_tokens: int = 4000) -> LLMResult:
        self.calls.append({"system": system, "user": user, "schema": schema})
        out = self.responder(system, user, schema) if self.responder else None
        if out is None:
            out = self._fixture(schema)
        if out is None:
            out = minimal_instance(schema)
        text = json.dumps(out)
        usage = {"input_tokens": tokens_of(system + user), "output_tokens": tokens_of(text)}
        return LLMResult(json=out, usage=usage, model=self.model, cost_usd=cost_of(self.model, usage),
                         billed=False, text=text, stop_reason="end_turn")

    def complete_many(self, requests: list[dict]) -> list[LLMResult | Exception]:
        return [self.complete(r["system"], r["user"], r["schema"], r.get("max_tokens", 4000)) for r in requests]


class AnthropicLLM:
    name = "anthropic"
    billed = True

    def __init__(self, model: str, effort: str = "medium", fallbacks: bool = True, client=None):
        self.model = model
        self.effort = effort
        self.fallbacks = fallbacks
        self.oauth = False
        self._client = client

    def client(self):
        if self._client is None:
            import anthropic  # lazy: optional dependency

            key = os.environ.get("ANTHROPIC_API_KEY")
            tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            if key:
                self._client = anthropic.Anthropic(api_key=key)
            elif tok:
                self.oauth = True
                self._client = anthropic.Anthropic(auth_token=tok,
                                                   default_headers={"anthropic-beta": "oauth-2025-04-20"})
            else:
                raise LLMError("no ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN")
        return self._client

    def _system(self, system: str) -> list[dict]:
        blocks = [{"type": "text", "text": system}]
        if self.oauth:
            blocks.insert(0, {"type": "text", "text": CLAUDE_CODE_SYSTEM})
        return blocks

    def _params(self, system: str, user: str, schema: dict, max_tokens: int) -> dict:
        return {"model": self.model, "max_tokens": max_tokens, "system": self._system(system),
                "messages": [{"role": "user", "content": user}], "thinking": {"type": "adaptive"},
                "output_config": {"effort": self.effort, "format": {"type": "json_schema", "schema": schema}}}

    def _result(self, msg, batch: bool = False) -> LLMResult:
        stop = getattr(msg, "stop_reason", None)
        if stop in ("refusal", "max_tokens"):
            raise LLMError(f"model stopped: {stop}")
        text = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text")
        try:
            data = json.loads(text)
        except ValueError as e:
            raise LLMError(f"model output is not JSON: {e}")
        u = msg.usage
        usage = {k: getattr(u, k, 0) or 0 for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                      "cache_creation_input_tokens")}
        served = getattr(msg, "model", self.model) or self.model
        cost = max(cost_of(self.model, usage, batch), cost_of(served, usage, batch))
        return LLMResult(json=data, usage=usage, model=served, cost_usd=cost, billed=True, text=text,
                         stop_reason=stop)

    def complete(self, system: str, user: str, schema: dict, max_tokens: int = 16000) -> LLMResult:
        c = self.client()
        p = self._params(system, user, schema, max_tokens)
        if self.fallbacks:
            msg = c.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **p)
        else:
            msg = c.messages.create(**p)
        return self._result(msg)

    def complete_many(self, requests: list[dict], poll_s: float = 30, timeout_s: float = 6 * 3600) -> list:
        c = self.client()
        reqs = [{"custom_id": f"r{i}", "params": self._params(r["system"], r["user"], r["schema"],
                                                              r.get("max_tokens", 16000))}
                for i, r in enumerate(requests)]
        batch = c.messages.batches.create(requests=reqs)
        end = time.time() + timeout_s
        while c.messages.batches.retrieve(batch.id).processing_status != "ended":
            if time.time() > end:
                raise LLMError("batch timed out")
            time.sleep(poll_s)
        out: list = [LLMError("missing result")] * len(requests)
        for res in c.messages.batches.results(batch.id):
            i = int(res.custom_id[1:])
            if res.result.type == "succeeded":
                try:
                    out[i] = self._result(res.result.message, batch=True)
                except LLMError as e:
                    out[i] = e
            else:
                out[i] = LLMError(f"batch item {res.result.type}")
        return out


class ClaudeCliLLM:
    name = "claude_cli"
    billed = True

    def __init__(self, model: str, effort: str = "medium", binary: str | None = None, timeout_s: float | None = None):
        self.model = model
        self.effort = effort
        self.binary = binary or os.environ.get("KH_WORKER_CLAUDE_BIN") or "claude"
        self.timeout_s = timeout_s or float(os.environ.get("KH_WORKER_CLI_TIMEOUT_S") or 900)
        self.max_budget_usd: float | None = None

    def complete(self, system: str, user: str, schema: dict, max_tokens: int = 16000) -> LLMResult:
        with tempfile.TemporaryDirectory(prefix="kh-worker-") as tmp:
            sp = os.path.join(tmp, "system.md")
            with open(sp, "w", encoding="utf-8") as f:
                f.write(system)
            cmd = [self.binary, "-p", "--output-format", "json", "--model", self.model, "--effort", self.effort,
                   "--json-schema", json.dumps(schema), "--system-prompt-file", sp, "--tools", "",
                   "--strict-mcp-config", "--setting-sources", "project", "--no-session-persistence"]
            if self.max_budget_usd is not None:
                cmd += ["--max-budget-usd", f"{self.max_budget_usd:.4f}"]
            p = subprocess.run(cmd, input=user, capture_output=True, text=True, cwd=tmp, timeout=self.timeout_s)
        lines = [ln for ln in (p.stdout or "").splitlines() if ln.strip()]
        if not lines:
            raise LLMError(f"claude CLI produced no output (exit {p.returncode}): {(p.stderr or '')[:300]}")
        try:
            env = json.loads(lines[-1])
        except ValueError as e:
            raise LLMError(f"claude CLI output is not JSON: {e}")
        data = env.get("structured_output")
        if data is None:
            try:
                data = json.loads(env.get("result") or "")
            except (TypeError, ValueError):
                raise LLMError("claude CLI returned no structured output")
        usage = env.get("usage") or {}
        return LLMResult(json=data, usage=usage, model=self.model, cost_usd=float(env.get("total_cost_usd") or 0),
                         billed=True, text=json.dumps(data))

    def complete_many(self, requests: list[dict]) -> list:
        out = []
        for r in requests:
            try:
                out.append(self.complete(r["system"], r["user"], r["schema"], r.get("max_tokens", 16000)))
            except Exception as e:  # noqa: BLE001
                out.append(e)
        return out


def make_llm(kind: str, model: str, effort: str = "medium", fallbacks: bool = True):
    k = (kind or "").lower()
    if k == "dry":
        return DryLLM(model or "dry")
    if k == "anthropic":
        return AnthropicLLM(model, effort, fallbacks)
    if k == "claude_cli":
        return ClaudeCliLLM(model, effort)
    raise ValueError(f"unknown LLM adapter '{kind}' (anthropic, claude_cli, dry)")


class Metered:
    """Budget-checked LLM: reserve an estimate, call, book the actual cost."""

    def __init__(self, llm, budget: Budget):
        self.llm = llm
        self.budget = budget
        self.model = getattr(llm, "model", "")
        self.name = getattr(llm, "name", "")
        self.billed = getattr(llm, "billed", False)

    def complete(self, system: str, user: str, schema: dict, max_tokens: int = 16000) -> LLMResult:
        est = estimate(self.model, tokens_of(system + user), max_tokens)
        hold = self.budget.reserve(est)
        if isinstance(self.llm, ClaudeCliLLM):
            self.llm.max_budget_usd = self.budget.remaining() + hold
        try:
            res = self.llm.complete(system, user, schema, max_tokens)
        except Exception:
            self.budget.release(hold)
            raise
        self.budget.book(hold, res.cost_usd, res.billed, {"model": res.model, "usage": res.usage})
        return res

    def complete_many(self, requests: list[dict]) -> list:
        est = sum(estimate(self.model, tokens_of(r["system"] + r["user"]), r.get("max_tokens", 16000), batch=True)
                  for r in requests)
        hold = self.budget.reserve(est)
        try:
            res = self.llm.complete_many(requests)
        except Exception:
            self.budget.release(hold)
            raise
        total = sum(r.cost_usd for r in res if isinstance(r, LLMResult))
        billed = any(isinstance(r, LLMResult) and r.billed for r in res)
        self.budget.book(hold, total, billed, {"model": self.model, "batch": True})
        return res
