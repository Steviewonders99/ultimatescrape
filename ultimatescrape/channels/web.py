"""Generic web channel — HTTP first, browser only when HTTP comes back thin.

The escalation rule matters for cost: a headless browser is roughly two orders of
magnitude more expensive in time and memory than an HTTP GET, so it is a fallback
triggered by evidence (an empty body, a JS-shell page, a 403) rather than a
default. ``force_browser`` overrides for sites known to need it.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

from ..fetch.http import Fetcher
from .base import BackendStatus, Channel, ChannelResult, Health

log = logging.getLogger("uscrape.web")

# Below this, an HTTP fetch almost certainly hit a JS shell rather than content.
THIN_WORD_COUNT = 120


class WebChannel(Channel):
    name = "web"
    backends = ("http", "browser")

    def __init__(self, fetcher: Fetcher | None = None, *, allow_browser: bool = True) -> None:
        self._fetcher = fetcher
        self._owns_fetcher = fetcher is None
        self.allow_browser = allow_browser
        self._browser = None

    def can_handle(self, url: str) -> bool:
        return urlparse(url).scheme in ("http", "https")

    async def health(self) -> list[BackendStatus]:
        from ..fetch import browser as browser_mod

        statuses = [BackendStatus("http", Health.OK, "httpx + selectolax", cost_hint_usd=0.0)]
        if browser_mod.available():
            statuses.append(
                BackendStatus("browser", Health.OK, "crawl4ai installed", cost_hint_usd=0.0)
            )
        else:
            statuses.append(
                BackendStatus(
                    "browser",
                    Health.UNCONFIGURED,
                    "uv pip install -e '.[[crawl]]' && crawl4ai-setup",
                )
            )
        return statuses

    async def _get_fetcher(self) -> Fetcher:
        if self._fetcher is None:
            self._fetcher = Fetcher()
        return self._fetcher

    async def aclose(self) -> None:
        if self._owns_fetcher and self._fetcher is not None:
            await self._fetcher.aclose()
            self._fetcher = None

    async def fetch(self, url: str, *, force_browser: bool = False, **_: Any) -> ChannelResult:
        attempted: list[str] = []
        http_result = None

        if not force_browser:
            attempted.append("http")
            fetcher = await self._get_fetcher()
            http_result = await fetcher.fetch(url)
            words = http_result.doc.word_count if http_result.doc else 0
            thin = http_result.ok and words < THIN_WORD_COUNT
            if http_result.ok and not thin:
                return self._from_http(url, http_result, attempted)

            reason = http_result.error or f"thin extraction ({words} words)"
            from ..fetch import browser as browser_mod

            # Escalate only when a browser can actually change the outcome.
            # Falling back to the HTTP result matters: a genuinely short page and
            # a hard 404 are both legitimate answers, and reporting them as
            # "browser not installed" hides what really happened.
            if not self.allow_browser or not browser_mod.available():
                if not http_result.ok:
                    return self._from_http(url, http_result, attempted)
                log.debug("[web] %s is thin (%d words) but no browser tier available", url, words)
                return self._from_http(url, http_result, attempted, note="thin extraction")
            log.debug("[web] escalating %s to browser: %s", url, reason)

        from ..fetch import browser as browser_mod

        if not browser_mod.available():
            return ChannelResult(
                url=url,
                ok=False,
                backend="none",
                attempted=attempted,
                error='browser tier requested but crawl4ai is not installed — '
                'uv pip install -e ".[crawl]" && crawl4ai-setup',
            )

        attempted.append("browser")
        async with browser_mod.BrowserFetcher() as bf:
            res = await bf.fetch(url)
        if not res.ok and http_result is not None and http_result.ok:
            # The browser did worse than plain HTTP; keep the better answer.
            return self._from_http(url, http_result, attempted, note="browser tier failed")
        return ChannelResult(
            url=url,
            ok=res.ok,
            backend="browser",
            attempted=attempted,
            markdown=res.doc.markdown if res.doc else "",
            error=res.error,
            data=res.as_dict(),
        )

    @staticmethod
    def _from_http(url, res, attempted: list[str], note: str | None = None) -> ChannelResult:
        return ChannelResult(
            url=url,
            ok=res.ok,
            backend="http",
            attempted=attempted,
            markdown=res.doc.markdown if res.doc else "",
            error=res.error,
            data={**res.as_dict(), **({"note": note} if note else {})},
        )
