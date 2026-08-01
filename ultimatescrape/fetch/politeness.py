"""Per-host concurrency, delay, and robots.txt — the layer nothing in the fleet had.

The existing fetchers only ever ran against six search results per request, so
they never needed this. A swarm that issues thousands of fetches does: without
per-host limits the first popular domain in the frontier gets hammered by every
worker at once, which is both how you get blocked and how you become the problem.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from ..config import settings

log = logging.getLogger("uscrape.politeness")


def host_of(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


@dataclass
class _HostState:
    sem: asyncio.Semaphore
    next_allowed: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    crawl_delay: float | None = None


class HostGate:
    """Bounds in-flight requests per host and spaces them out.

    ``crawl-delay`` from robots.txt wins over the configured default when it is
    larger — a site asking for 10s gets 10s.
    """

    def __init__(
        self,
        per_host: int | None = None,
        delay_s: float | None = None,
    ) -> None:
        self.per_host = per_host or settings.per_host_concurrency
        self.delay_s = settings.per_host_delay_s if delay_s is None else delay_s
        self._hosts: dict[str, _HostState] = {}
        self._lock = asyncio.Lock()

    async def _state(self, host: str) -> _HostState:
        async with self._lock:
            if host not in self._hosts:
                self._hosts[host] = _HostState(sem=asyncio.Semaphore(self.per_host))
            return self._hosts[host]

    def set_crawl_delay(self, host: str, delay: float | None) -> None:
        state = self._hosts.get(host)
        if state and delay:
            state.crawl_delay = delay

    class _Slot:
        def __init__(self, gate: HostGate, host: str) -> None:
            self.gate, self.host = gate, host
            self.state: _HostState | None = None

        async def __aenter__(self) -> None:
            self.state = await self.gate._state(self.host)
            await self.state.sem.acquire()
            async with self.state.lock:
                delay = max(self.gate.delay_s, self.state.crawl_delay or 0.0)
                wait = self.state.next_allowed - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self.state.next_allowed = time.monotonic() + delay

        async def __aexit__(self, *exc: object) -> None:
            if self.state:
                self.state.sem.release()

    def slot(self, url: str) -> _Slot:
        return HostGate._Slot(self, host_of(url))


class RobotsCache:
    """robots.txt fetched once per host and cached for the process lifetime.

    Fails open: an unreachable or malformed robots.txt means crawl. That matches
    every major crawler's behaviour, and a 500 on robots.txt is not consent
    withdrawal. A 401/403 on robots.txt, though, is treated as a full disallow —
    that is the RFC 9309 recommendation.
    """

    def __init__(self, user_agent: str | None = None) -> None:
        self.user_agent = user_agent or settings.user_agent
        self._cache: dict[str, RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global = asyncio.Lock()

    async def _lock_for(self, host: str) -> asyncio.Lock:
        async with self._global:
            return self._locks.setdefault(host, asyncio.Lock())

    async def _load(self, client: httpx.AsyncClient, url: str) -> RobotFileParser | None:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host in self._cache:
            return self._cache[host]

        async with await self._lock_for(host):
            if host in self._cache:
                return self._cache[host]
            robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
            parser: RobotFileParser | None = None
            try:
                r = await client.get(robots_url, timeout=10.0, follow_redirects=True)
                if r.status_code in (401, 403):
                    parser = RobotFileParser()
                    parser.disallow_all = True
                elif r.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(r.text.splitlines())
                # 404 and 5xx → None → allow.
            except Exception as exc:  # noqa: BLE001
                log.debug("robots.txt unreachable for %s (%s); allowing", host, exc)
            self._cache[host] = parser
            return parser

    async def allowed(self, client: httpx.AsyncClient, url: str) -> tuple[bool, float | None]:
        """Returns ``(allowed, crawl_delay_seconds)``."""
        if not settings.respect_robots:
            return True, None
        parser = await self._load(client, url)
        if parser is None:
            return True, None
        try:
            ok = parser.can_fetch(self.user_agent, url)
            delay = parser.crawl_delay(self.user_agent)
            return ok, float(delay) if delay else None
        except Exception:  # noqa: BLE001
            return True, None
