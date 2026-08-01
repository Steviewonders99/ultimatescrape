"""Headless-browser tier, backed by Crawl4AI.

Crawl4AI is an optional dependency: the HTTP tier handles most of the web, and
pulling Playwright + Patchright into every install is a bad trade. This module
imports lazily and reports a clear reason when unavailable rather than blowing up
at import time.

Why Crawl4AI rather than raw Playwright: it already ships the three things this
tier needs and would otherwise take weeks to get right — a memory-adaptive
dispatcher with a browser pool, per-domain rate limiting with 429/503 backoff,
and an undetected-Chromium mode (Patchright) with proxy chains and fallback. It
also routes LLM extraction through LiteLLM, whose ``base_url`` we can point at
Kimi so the browser tier and the swarm share one model.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from ..config import settings
from .extract import Extracted, now_iso
from .http import FetchResult

log = logging.getLogger("uscrape.browser")

_UNAVAILABLE = (
    "crawl4ai is not installed. Install the browser tier with:\n"
    '  uv pip install -e ".[crawl]" && crawl4ai-setup'
)


def available() -> bool:
    try:
        import crawl4ai  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class BrowserOptions:
    headless: bool = True
    undetected: bool = False
    proxy: str | None = None
    # Path to a Playwright storage_state JSON. This is how a logged-in session
    # (LinkedIn's li_at cookie, for example) is carried into the browser without
    # the credentials ever touching this codebase.
    storage_state: str | None = None
    user_data_dir: str | None = None
    session_id: str | None = None
    wait_for: str | None = None
    js_code: str | None = None
    page_timeout_ms: int = 45_000
    word_count_threshold: int = 10


class BrowserFetcher:
    """Thin adapter so browser results are the same ``FetchResult`` shape as HTTP."""

    def __init__(self, options: BrowserOptions | None = None) -> None:
        if not available():
            raise RuntimeError(_UNAVAILABLE)
        self.options = options or BrowserOptions()
        self._crawler = None

    async def __aenter__(self) -> BrowserFetcher:
        from crawl4ai import AsyncWebCrawler, BrowserConfig

        opts = self.options
        cfg = BrowserConfig(
            headless=opts.headless,
            browser_mode="undetected" if opts.undetected else "dedicated",
            proxy=opts.proxy,
            storage_state=opts.storage_state,
            user_data_dir=opts.user_data_dir,
            use_persistent_context=bool(opts.user_data_dir),
            user_agent=settings.user_agent,
        )
        self._crawler = AsyncWebCrawler(config=cfg)
        await self._crawler.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._crawler is not None:
            await self._crawler.__aexit__(*exc)
            self._crawler = None

    def _run_config(self):
        from crawl4ai import CacheMode, CrawlerRunConfig
        from crawl4ai.content_filter_strategy import PruningContentFilter
        from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

        opts = self.options
        return CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            markdown_generator=DefaultMarkdownGenerator(
                content_filter=PruningContentFilter(threshold=0.45, threshold_type="dynamic")
            ),
            word_count_threshold=opts.word_count_threshold,
            page_timeout=opts.page_timeout_ms,
            wait_for=opts.wait_for,
            js_code=opts.js_code,
            session_id=opts.session_id,
            scan_full_page=True,
        )

    @staticmethod
    def _to_result(url: str, res) -> FetchResult:
        if not getattr(res, "success", False):
            return FetchResult(
                url=url,
                ok=False,
                status=getattr(res, "status_code", None),
                error=getattr(res, "error_message", "crawl failed"),
            )
        md_obj = getattr(res, "markdown", None)
        markdown = ""
        if md_obj is not None:
            markdown = (
                getattr(md_obj, "fit_markdown", None)
                or getattr(md_obj, "raw_markdown", None)
                or str(md_obj)
            )
        meta = getattr(res, "metadata", None) or {}
        links = getattr(res, "links", None) or {}
        internal = [ln.get("href") for ln in links.get("internal", []) if ln.get("href")]
        external = [ln.get("href") for ln in links.get("external", []) if ln.get("href")]
        doc = Extracted(
            url=getattr(res, "url", url),
            title=meta.get("title"),
            description=meta.get("description"),
            markdown=markdown,
            text=markdown,
            word_count=len(markdown.split()),
            links=(internal + external)[:400],
            strategy="browser",
        )
        return FetchResult(
            url=url,
            ok=True,
            status=getattr(res, "status_code", 200),
            final_url=getattr(res, "url", url),
            content_type="text/html",
            doc=doc,
            fetched_at=now_iso(),
        )

    async def fetch(self, url: str) -> FetchResult:
        res = await self._crawler.arun(url=url, config=self._run_config())
        return self._to_result(url, res)

    async def fetch_many(
        self, urls: Sequence[str], *, max_sessions: int = 8, per_host_delay: tuple[float, float] = (1.0, 3.0)
    ) -> list[FetchResult]:
        from crawl4ai import MemoryAdaptiveDispatcher, RateLimiter

        dispatcher = MemoryAdaptiveDispatcher(
            memory_threshold_percent=85.0,
            max_session_permit=max_sessions,
            rate_limiter=RateLimiter(
                base_delay=per_host_delay,
                max_delay=60.0,
                max_retries=3,
                rate_limit_codes=[429, 503],
            ),
        )
        results = await self._crawler.arun_many(
            urls=list(urls), config=self._run_config(), dispatcher=dispatcher
        )
        return [self._to_result(getattr(r, "url", ""), r) for r in results]
