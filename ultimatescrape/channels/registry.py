"""URL → channel dispatch, with the generic web channel as the terminal fallback."""

from __future__ import annotations

import asyncio
from typing import Any

from ..fetch.http import Fetcher
from .base import BackendStatus, Channel, ChannelResult, Health
from .linkedin import LinkedInChannel
from .web import WebChannel


class ChannelRegistry:
    """Holds channels in priority order. The web channel always sits last so any
    URL nothing else claims still gets fetched."""

    def __init__(self, fetcher: Fetcher | None = None, *, allow_browser: bool = True) -> None:
        self._web = WebChannel(fetcher, allow_browser=allow_browser)
        self._channels: list[Channel] = [LinkedInChannel(), self._web]

    def register(self, channel: Channel, *, first: bool = True) -> None:
        self._channels.insert(0 if first else len(self._channels) - 1, channel)

    def route(self, url: str) -> Channel:
        for channel in self._channels:
            if channel.can_handle(url):
                return channel
        return self._web

    async def fetch(self, url: str, **kwargs: Any) -> ChannelResult:
        return await self.route(url).fetch(url, **kwargs)

    async def fetch_many(self, urls: list[str], **kwargs: Any) -> list[ChannelResult]:
        return list(await asyncio.gather(*(self.fetch(u, **kwargs) for u in urls)))

    async def doctor(self) -> dict[str, list[dict]]:
        """Health of every backend of every channel. Run before a large job."""
        report: dict[str, list[dict]] = {}
        for channel in self._channels:
            try:
                statuses = await channel.health()
            except Exception as exc:  # noqa: BLE001
                statuses = [BackendStatus("?", Health.UNAVAILABLE, str(exc))]
            report[channel.name] = [s.as_dict() for s in statuses]
        return report

    async def aclose(self) -> None:
        await asyncio.gather(
            *(c.aclose() for c in self._channels), return_exceptions=True
        )
