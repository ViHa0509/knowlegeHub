"""Spend caps (spec §4.6 locks 1/2) and the metered LLM door."""
import pytest

from kh_worker import budget as B
from kh_worker.budget import Budget, BudgetExceeded
from kh_worker.llm import ClaudeCliLLM, LLMResult, Metered

from .conftest import FakeLLM


def test_prices_per_mtok_exactly_as_spec():
    assert B.PRICES == {"opus-5-5": (4.0, 20.0), "opus-5": (5.0, 25.0), "sonnet-5": (2.0, 10.0),
                        "haiku-4-5": (1.0, 5.0), "fable-5": (10.0, 50.0)}
    assert B.CHARS_PER_TOKEN == 3


def test_price_lookup_longest_key_and_unknown_is_most_expensive():
    assert B.price_of("claude-opus-5-5") == (4.0, 20.0)
    assert B.price_of("claude-opus-5") == (5.0, 25.0)
    assert B.price_of("claude-haiku-4-5-20260101") == (1.0, 5.0)
    assert B.price_of("mystery-model") == (10.0, 50.0) == B.price_of(None)


def test_tokens_and_estimate():
    assert B.tokens_of("x" * 30) == 10 and B.tokens_of("") == 1
    # all input at input price + the whole max_tokens at output price
    assert B.estimate("opus-5-5", 1_000_000, 1_000_000) == pytest.approx(24.0)
    assert B.estimate("opus-5-5", 1_000_000, 1_000_000, batch=True) == pytest.approx(12.0)


def test_cost_of_usage_with_cache_factors():
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000,
             "cache_creation_input_tokens": 1_000_000}
    # 4 + 0.4 (read 10%) + 5 (write 125%) + 20
    assert B.cost_of("opus-5-5", usage) == pytest.approx(29.4)
    assert B.cost_of("opus-5-5", usage, batch=True) == pytest.approx(14.7)
    assert B.cost_of("opus-5-5", {}) == 0


def test_run_cap_refuses_with_reserved_holds():
    b = Budget(1.0)
    h = b.reserve(0.6)
    with pytest.raises(BudgetExceeded):
        b.reserve(0.5)  # 0 spent + 0.6 reserved + 0.5 > 1.0
    b.book(h, 0.2, billed=True)
    assert b.reserved == 0 and b.run_spent == pytest.approx(0.2)
    b.reserve(0.8)  # 0.2 + 0 + 0.8 == cap is allowed
    with pytest.raises(BudgetExceeded):
        b.reserve(0.01)


def test_month_cap_and_backfill():
    b = Budget(3.0, cap_month_usd=60.0, month_spent_usd=59.5)
    with pytest.raises(BudgetExceeded, match="month"):
        b.reserve(1.0)
    bf = Budget(3.0, 60.0, 59.5, backfill=True, cap_backfill_usd=80.0)
    assert bf.cap_run == 80.0
    h = bf.reserve(10.0)
    bf.book(h, 10.0, billed=True)
    assert bf.month_spent == 59.5 and bf.billed_spent == 10.0  # backfill excluded from the month


def test_remaining_is_min_of_run_and_month():
    assert Budget(3.0, 60.0, 59.0).remaining() == pytest.approx(1.0)
    assert Budget(3.0, 60.0, 59.0, backfill=True, cap_backfill_usd=80.0).remaining() == 80.0
    assert Budget(1.0, 60.0, 70.0).remaining() == 0.0


def test_dry_cost_counts_toward_run_cap_but_is_not_billed():
    spent = []
    b = Budget(1.0, on_spend=lambda usd, info: spent.append(usd))
    b.book(b.reserve(0.1), 0.9, billed=False)
    assert b.run_spent == pytest.approx(0.9) and b.billed_spent == 0 and b.month_spent == 0 and spent == []
    with pytest.raises(BudgetExceeded):
        b.reserve(0.2)
    b.book(0.0, 0.05, billed=True, info={"model": "m"})
    assert spent == [0.05]
    s = b.summary()
    assert s["calls"] == 2 and s["cap_run_usd"] == 1.0 and s["billed_usd"] == 0.05


def test_metered_reserves_then_books_actual_and_releases_on_error():
    llm = FakeLLM([{"a": 1}, RuntimeError("boom")], cost=0.02)
    b = Budget(3.0)
    m = Metered(llm, b)
    m.complete("s", "u", {}, 100)
    assert b.run_spent == pytest.approx(0.02) and b.reserved == 0 and b.calls == 1
    with pytest.raises(RuntimeError):
        m.complete("s", "u", {}, 100)
    assert b.reserved == 0 and b.calls == 1


def test_metered_refuses_before_calling_when_estimate_exceeds_cap():
    llm = FakeLLM([{"a": 1}])
    with pytest.raises(BudgetExceeded):
        Metered(llm, Budget(0.01)).complete("s", "u", {}, 16000)  # 16000 * 20 / 1e6 = 0.32 > 0.01
    assert llm.calls == []


def test_metered_batch_uses_half_price_estimate():
    llm = FakeLLM([{"a": 1}, {"a": 2}], cost=0.01)
    b = Budget(0.17)  # 2 x 8000 output tokens: 0.32 at full price, 0.16 at batch price
    res = Metered(llm, b).complete_many([{"system": "s", "user": "u", "schema": {}, "max_tokens": 8000}] * 2)
    assert len(res) == 2 and b.run_spent == pytest.approx(0.02) and b.calls == 1 and b.reserved == 0


def test_metered_sets_cli_max_budget(monkeypatch):
    cli = ClaudeCliLLM("claude-opus-5-5", binary="claude", timeout_s=5)
    seen = {}

    def fake_complete(system, user, schema, max_tokens=16000):
        seen["max"] = cli.max_budget_usd
        return LLMResult(json={}, cost_usd=0.0, billed=True)

    monkeypatch.setattr(cli, "complete", fake_complete)
    Metered(cli, Budget(3.0)).complete("s", "u", {}, 1000)
    assert seen["max"] == pytest.approx(3.0)  # remaining (cap - hold) + the hold itself
