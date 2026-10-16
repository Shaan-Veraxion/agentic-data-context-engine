"""The run's budget: every model call one pipeline run makes goes through RunGateway, which counts calls and cost and
refuses calls once the run's limits are reached (BudgetExceeded, which agents treat like an unavailable provider:
the statement or table is skipped and tried again on the next run).

The supervisor sees what was spent, so it can stop asking the model when the budget is nearly used.
"""
from __future__ import annotations

from typing import Any


class RunGateway:
    def __init__(self, inner: Any, *, max_usd: float | None = None, max_calls: int | None = None) -> None:
        self.inner = inner
        self.max_usd = max_usd
        self.max_calls = max_calls
        self.calls = 0  # calls answered (live, memo or replay)
        self.spent_usd = 0.0

    def __getattr__(self, name: str) -> Any:  # mode, recordings, config ... of the gateway it wraps
        return getattr(self.inner, name)

    def left(self) -> dict[str, Any]:
        return {"calls": self.calls, "spent_usd": round(self.spent_usd, 4), "max_usd": self.max_usd,
                "max_calls": self.max_calls}

    async def call(self, route: str, inputs: dict[str, Any], output_model: Any, **kw: Any) -> Any:
        from adce_gateway.gateway import BudgetExceeded

        if self.max_usd is not None and self.spent_usd >= self.max_usd:
            raise BudgetExceeded(f"run budget {self.max_usd} USD reached ({self.spent_usd:.4f} USD spent)")
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise BudgetExceeded(f"run limit of {self.max_calls} model calls reached")
        r = await self.inner.call(route, inputs, output_model, **kw)
        self.calls += 1
        self.spent_usd += float(getattr(r, "cost_usd", 0.0) or 0.0)
        return r
