"""Channel abstraction — the one genuinely good idea in Agent-Reach.

A channel owns one platform. It declares which URLs it can handle, exposes an
ordered list of *backends* (concrete access paths, best first), and can be health-
checked without doing any real work. The dispatcher routes a URL to the first
channel that claims it, and the channel walks its backends until one succeeds.

The payoff is that "LinkedIn access" becomes a configuration decision rather than
a code change: add a paid vendor key and it becomes tier 1; remove it and the
system silently degrades to the self-hosted session, then to public-page reading.
``uscrape doctor`` tells you which tiers are live before a run, instead of
discovering it 40 minutes in.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Health(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    UNCONFIGURED = "unconfigured"
    UNAVAILABLE = "unavailable"


@dataclass
class BackendStatus:
    name: str
    health: Health
    detail: str = ""
    # Rough per-record cost, for picking a tier under a budget.
    cost_hint_usd: float | None = None
    # True when this path carries account-ban or ToS risk the operator must accept.
    risk: bool = False

    def as_dict(self) -> dict:
        return {
            "backend": self.name,
            "health": self.health.value,
            "detail": self.detail,
            "cost_hint_usd": self.cost_hint_usd,
            "risk": self.risk,
        }


@dataclass
class ChannelResult:
    url: str
    ok: bool
    backend: str
    data: dict[str, Any] = field(default_factory=dict)
    markdown: str = ""
    error: str | None = None
    attempted: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "ok": self.ok,
            "backend": self.backend,
            "attempted": self.attempted,
            "error": self.error,
            "data": self.data,
            "markdown": self.markdown[:20_000],
        }


class Channel(ABC):
    name: str = "channel"
    #: Ordered best-first. Tiers absent from config are skipped, not failed.
    backends: tuple[str, ...] = ()

    @abstractmethod
    def can_handle(self, url: str) -> bool: ...

    @abstractmethod
    async def health(self) -> list[BackendStatus]: ...

    @abstractmethod
    async def fetch(self, url: str, **kwargs: Any) -> ChannelResult: ...

    async def aclose(self) -> None:
        return None
