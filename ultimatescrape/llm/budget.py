"""Run-scoped spend meter.

Every existing swarm in this fleet tracks cost by printing a total at the end,
which is useless for stopping a runaway. This ledger is shared by every agent in
a run and raises as soon as the ceiling is crossed, so a bad prompt costs one
call's overrun rather than the whole budget.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..config import settings


class BudgetExceeded(RuntimeError):
    """Raised when a run's cumulative spend crosses its ceiling."""


@dataclass
class Ledger:
    limit_usd: float = field(default_factory=lambda: settings.max_run_cost_usd)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    failures: int = 0
    by_model: dict[str, dict[str, float]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def record(self, model: str, usage: dict) -> float:
        """Add one call's usage. Returns the incremental cost in USD."""
        p_in = int(usage.get("prompt_tokens") or 0)
        p_out = int(usage.get("completion_tokens") or 0)
        reason = int(
            (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
        )
        rate_in, rate_out = settings.price(model)
        delta = (p_in * rate_in + p_out * rate_out) / 1_000_000

        async with self._lock:
            self.prompt_tokens += p_in
            self.completion_tokens += p_out
            self.reasoning_tokens += reason
            self.cost_usd += delta
            self.calls += 1
            slot = self.by_model.setdefault(
                model, {"calls": 0, "prompt": 0, "completion": 0, "cost_usd": 0.0}
            )
            slot["calls"] += 1
            slot["prompt"] += p_in
            slot["completion"] += p_out
            slot["cost_usd"] += delta
            over = self.cost_usd >= self.limit_usd > 0

        if over:
            raise BudgetExceeded(
                f"run spend ${self.cost_usd:.4f} reached the ${self.limit_usd:.2f} ceiling "
                f"after {self.calls} calls; raise USCRAPE_MAX_RUN_COST_USD to continue"
            )
        return delta

    async def note_failure(self) -> None:
        async with self._lock:
            self.failures += 1

    def remaining(self) -> float:
        return max(0.0, self.limit_usd - self.cost_usd) if self.limit_usd > 0 else float("inf")

    def summary(self) -> dict:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": round(self.cost_usd, 4),
            "limit_usd": self.limit_usd,
            "by_model": {
                m: {**v, "cost_usd": round(v["cost_usd"], 4)} for m, v in self.by_model.items()
            },
        }
