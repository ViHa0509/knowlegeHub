"""Worker-side spend caps (spec §4.6, locks 1/2).

Before each call reserve a pessimistic estimate (all input tokens at input price + the whole max_tokens at output
price, x0.5 for Message Batches); refuse (`BudgetExceeded`) if run spend + reserved + estimate > run cap (or the
backfill cap for --backfill), or month spend + reserved + estimate > month cap (backfill runs are excluded from the
month). After the call book the actual cost from `usage` (cache read 10%, cache write 125%) and release the hold.
Without a token counter, 3 chars/token. Dry adapter cost counts toward the run cap but is not billed.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

# USD per million tokens (input, output)
PRICES = {
    "opus-5-5": (4.0, 20.0),
    "opus-5": (5.0, 25.0),
    "sonnet-5": (2.0, 10.0),
    "haiku-4-5": (1.0, 5.0),
    "fable-5": (10.0, 50.0),
}
MOST_EXPENSIVE = max(PRICES.values(), key=lambda p: p[1])
CHARS_PER_TOKEN = 3


class BudgetExceeded(Exception):
    pass


def price_of(model: str | None) -> tuple[float, float]:
    m = (model or "").lower()
    for key in sorted(PRICES, key=len, reverse=True):
        if key in m:
            return PRICES[key]
    return MOST_EXPENSIVE


def tokens_of(text: str) -> int:
    return max(1, len(text or "") // CHARS_PER_TOKEN)


def estimate(model: str, input_tokens: int, max_tokens: int, batch: bool = False) -> float:
    pi, po = price_of(model)
    usd = (input_tokens * pi + max_tokens * po) / 1_000_000
    return usd * (0.5 if batch else 1.0)


def cost_of(model: str, usage: dict, batch: bool = False) -> float:
    pi, po = price_of(model)
    u = usage or {}
    inp = float(u.get("input_tokens") or 0)
    out = float(u.get("output_tokens") or 0)
    cr = float(u.get("cache_read_input_tokens") or 0)
    cw = float(u.get("cache_creation_input_tokens") or 0)
    usd = (inp * pi + cr * pi * 0.10 + cw * pi * 1.25 + out * po) / 1_000_000
    return usd * (0.5 if batch else 1.0)


class Budget:
    def __init__(self, cap_run_usd: float, cap_month_usd: float = float("inf"), month_spent_usd: float = 0.0,
                 backfill: bool = False, cap_backfill_usd: float | None = None,
                 on_spend: Optional[Callable[[float, dict], None]] = None):
        self.cap_run = float(cap_backfill_usd if (backfill and cap_backfill_usd is not None) else cap_run_usd)
        self.cap_month = float(cap_month_usd)
        self.month_spent = float(month_spent_usd or 0.0)
        self.backfill = backfill
        self.run_spent = 0.0
        self.billed_spent = 0.0
        self.reserved = 0.0
        self.calls = 0
        self.on_spend = on_spend
        self._lock = threading.Lock()

    def remaining(self) -> float:
        left = self.cap_run - self.run_spent - self.reserved
        if not self.backfill:
            left = min(left, self.cap_month - self.month_spent - self.reserved)
        return max(0.0, left)

    def reserve(self, usd: float) -> float:
        with self._lock:
            if self.run_spent + self.reserved + usd > self.cap_run:
                raise BudgetExceeded(f"run cap {self.cap_run} USD would be exceeded "
                                     f"(spent {self.run_spent:.4f}, reserved {self.reserved:.4f}, next {usd:.4f})")
            if not self.backfill and self.month_spent + self.reserved + usd > self.cap_month:
                raise BudgetExceeded(f"month cap {self.cap_month} USD would be exceeded")
            self.reserved += usd
            return usd

    def book(self, hold: float, actual: float, billed: bool, info: dict | None = None) -> None:
        with self._lock:
            self.reserved = max(0.0, self.reserved - hold)
            self.run_spent += actual
            self.calls += 1
            if billed:
                self.billed_spent += actual
                if not self.backfill:
                    self.month_spent += actual
        if billed and self.on_spend is not None:
            self.on_spend(actual, info or {})

    def release(self, hold: float) -> None:
        with self._lock:
            self.reserved = max(0.0, self.reserved - hold)

    def summary(self) -> dict:
        return {"run_usd": round(self.run_spent, 6), "billed_usd": round(self.billed_spent, 6),
                "cap_run_usd": self.cap_run, "cap_month_usd": self.cap_month,
                "month_usd": round(self.month_spent, 6), "calls": self.calls, "backfill": self.backfill}
